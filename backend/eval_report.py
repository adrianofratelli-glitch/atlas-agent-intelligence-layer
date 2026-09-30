"""Aggregate task outcomes + real model-call ledgers exported as a JSON array.

Each row requires passed: boolean (from task assertions), llm_calls: array and
latency_ms: number. Missing prices are reported, never silently treated as free.
"""
import argparse
import json
import math


def summarize(rows):
    if not rows:
        raise ValueError('At least one evaluation result is required')
    for row in rows:
        if not isinstance(row.get('passed'), bool) or not isinstance(row.get('llm_calls'), list):
            raise ValueError('Each result requires passed:boolean and llm_calls:list')
        if not isinstance(row.get('latency_ms'), (float, int)) or not math.isfinite(row['latency_ms']) or row['latency_ms'] < 0:
            raise ValueError('Each result requires a finite nonnegative latency_ms')
    passed = sum(row['passed'] for row in rows)
    calls = [c for row in rows for c in row['llm_calls']]
    known = [c['estimated_cost_usd'] for c in calls if c.get('estimated_cost_usd') is not None]
    complete = len(known) == len(calls)
    total = sum(known) if complete else None
    latency = sorted(row['latency_ms'] for row in rows)
    return {'total': len(rows), 'passed': passed, 'pass_rate': passed/len(rows),
            'p95_ms': latency[math.ceil(.95*len(rows))-1], 'llm_calls': len(calls),
            'cost_coverage': len(known)/len(calls) if calls else 1,
            'estimated_cost_usd': total, 'known_cost_usd': sum(known),
            'cost_per_success_usd': total/passed if total is not None and passed else None,
            'measurement_scope': 'Task assertions supplied by evaluation; observed Grove blended estimates'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='JSON array of task outcomes and real call ledgers')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    with open(args.input) as f:
        report = summarize(json.load(f))
    with open(args.output, 'w') as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))
