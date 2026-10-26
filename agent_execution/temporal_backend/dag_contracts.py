"""Bounded opaque DAG wire protocol; safe to import inside the Workflow sandbox."""
import re

PARENT='PythonAICoordinatorDAGV1'
CHILD='PythonAICoordinatorNodeV1'
SNAPSHOT='pythonai_dag_snapshot_v1'
RUN='pythonai_dag_run_node_v1'
FENCE='pythonai_dag_fence_node_v1'
STOP='pythonai_dag_stop_v1'


def envelope(value,child=False):
    keys={'graph_id','node_id'} if child else {'graph_id'}
    if not isinstance(value,dict) or set(value)!=keys or any(
        not isinstance(v,str) or not re.fullmatch('[a-f0-9]{32}',v) for v in value.values()):
        raise ValueError('Invalid DAG reference')
    return value


def probe(value):
    if not isinstance(value,dict) or set(value)!={'done','stop','ready','concurrency'}:
        raise ValueError('Invalid DAG probe')
    if type(value['done']) is not bool or type(value['stop']) is not bool:
        raise ValueError('Invalid DAG state')
    if type(value['concurrency']) is not int or not 1<=value['concurrency']<=8:
        raise ValueError('Invalid DAG concurrency')
    ids=value['ready']
    if not isinstance(ids,list) or len(ids)>128 or any(
        not isinstance(v,str) or not re.fullmatch('[a-f0-9]{32}',v) for v in ids) or len(set(ids))!=len(ids):
        raise ValueError('Invalid DAG ready nodes')
    if (value['stop'] or value['done']) and ids:raise ValueError('Stopped DAG cannot start nodes')
    return value


def node_result(value):
    if not isinstance(value,dict) or set(value)!={'graph_id','node_id','cancelled'} or type(value['cancelled']) is not bool:
        raise ValueError('Invalid DAG node result')
    envelope({k:value[k] for k in ('graph_id','node_id')},child=True)
    return value
