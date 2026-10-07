"""Source-aware literal and range normalization shared by staged replay."""
import re


def poetry_constraint(value):
    parts=[]
    for clause in value.split(','):
        clause=clause.strip()
        match=re.fullmatch(r'([\^~])(\d+(?:\.\d+){0,2})',clause)
        if not match:
            parts.append(clause);continue
        operator,version=match.groups()
        numbers=[int(x) for x in version.split('.')]
        lower=numbers+[0]*(3-len(numbers))
        if operator=='~':index=1 if len(numbers)>1 else 0
        else:index=next((i for i,x in enumerate(numbers) if x),len(numbers)-1)
        upper=lower[:];upper[index]+=1
        for i in range(index+1,3):upper[i]=0
        parts.extend(['>='+'.'.join(map(str,lower)),'<'+'.'.join(map(str,upper))])
    return ','.join(parts)


def normalize_source_spec(event,spec):
    path=event.get('source_manifest_path') or event.get('path','')
    if event.get('ecosystem')=='PyPI' and path.endswith('pyproject.toml'):
        if spec=='*':return '>=0'
        return poetry_constraint(spec)
    return spec


def maven_literal(spec):
    if not spec or any(x in spec for x in ('$','[',']','(',')',',','+','*','/',' ',':')):
        return False
    if spec.upper() in {'LATEST','RELEASE'} or spec.startswith('latest.'):
        return False
    return bool(re.fullmatch(r'[0-9][0-9A-Za-z_.-]*',spec))
