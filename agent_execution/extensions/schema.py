"""Offline JSON Schema validation with limits on untrusted schemas and payloads."""
import json
from jsonschema import Draft202012Validator


def validator(schema):
    if not isinstance(schema,dict) or len(json.dumps(schema,allow_nan=False).encode())>32768:
        raise ValueError('Schema must be an object of at most 32 KiB')
    count=0
    def walk(value,depth=0):
        nonlocal count
        count+=1
        if count>2000 or depth>24:raise ValueError('Schema complexity limit')
        if isinstance(value,dict):
            # No fetching, recursive references or attacker-controlled regex execution.
            if any(k in value for k in ('$ref','$dynamicRef','pattern','patternProperties')):
                raise ValueError('References and regex schemas require a trusted adapter')
            for child in value.values():walk(child,depth+1)
        elif isinstance(value,list):
            for child in value:walk(child,depth+1)
    walk(schema);Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(value,schema):
    data=json.dumps(value,allow_nan=False)
    if len(data.encode())>1024*1024:raise ValueError('Payload exceeds 1 MiB')
    # Bound instance recursion before passing it to the schema implementation.
    def walk(item,depth=0):
        if depth>32:raise ValueError('Payload depth limit')
        if isinstance(item,dict):
            for child in item.values():walk(child,depth+1)
        elif isinstance(item,list):
            for child in item:walk(child,depth+1)
    walk(value)
    validator(schema).validate(value)
