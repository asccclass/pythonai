"""Label-based precision/recall; an empty set never counts as a perfect retrieval."""
def evaluate_retrieval(cases,search):
    if not cases:raise ValueError('Retrieval evaluation requires labeled cases')
    rows=[]
    for case in cases:
        expected=set(case['expected']);forbidden=set(case.get('forbidden',[]));found=set(search(case['query']))
        correct=len(found&expected)
        rows.append({'id':case['id'],'precision':correct/len(found) if found else 0,
            'recall':correct/len(expected) if expected else 0,'forbidden_hits':len(found&forbidden),'retrieved':sorted(found)})
    return {'cases':rows,'precision':sum(r['precision'] for r in rows)/len(rows),
        'recall':sum(r['recall'] for r in rows)/len(rows),'forbidden_hits':sum(r['forbidden_hits'] for r in rows)}
