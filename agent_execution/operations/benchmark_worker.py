import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


class Result(unittest.TestResult):
    def startTest(self,test):super().startTest(test);self.started=time.monotonic()
    def addSuccess(self,test):super().addSuccess(test)


def main(path):
    root=Path(path).parent
    os.environ.update(AGENT_TRACE_DB=str(root/'traces.db'),AGENT_TASK_DB=str(root/'tasks.db'),
        AGENT_EXTENSION_DB=str(root/'extensions.db'),AGENT_IMPROVEMENT_DB=str(root/'improvements.db'))
    suite_path=Path(__file__).with_name('suite.json');suite=json.loads(suite_path.read_text(encoding='utf-8'));rows=[]
    for item in suite:
        started=time.monotonic();result=Result();unittest.defaultTestLoader.loadTestsFromName(item['test']).run(result)
        rows.append({**item,'passed':result.wasSuccessful() and result.testsRun==1 and not result.skipped,
            'elapsed':time.monotonic()-started,'failure_count':len(result.failures)+len(result.errors),'skipped':len(result.skipped)})
    success=sum(row['passed'] for row in rows);recoveries=[row for row in rows if row.get('recovery')]
    import hashlib
    report={'version':1,'suite_sha256':hashlib.sha256(suite_path.read_bytes()).hexdigest(),'layer':'offline/native fixtures; no real provider/platform',
        'passed':success==len(rows),'completed_cases':success,'case_count':len(rows),'completion_rate':success/len(rows),
        'recovery_success_rate':sum(row['passed'] for row in recoveries)/len(recoveries),'elapsed':sum(row['elapsed'] for row in rows),
        'provider_cost_usd':None,'provider_cost_note':'No external provider called; billing/model quality not measured','cases':rows}
    Path(path).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main(sys.argv[1])
