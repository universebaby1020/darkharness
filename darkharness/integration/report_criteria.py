"""Declarative JSON criteria from authenticated controller configuration only.

No track vocabulary, executable loading, eval, or model-selected parser. Paths
are JSON key/index lists (not expressions). Request revision is mandatory.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Mapping


def _equal(actual, expected):
    if isinstance(actual, dict) and isinstance(expected, Mapping):
        return actual.keys() == expected.keys() and all(_equal(actual[k], expected[k]) for k in actual)
    if isinstance(actual, list) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(_equal(a, b) for a, b in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected
from .mailbox import IntegrationError


def _select(value, path):
    if not isinstance(path, (list, tuple)):
        raise IntegrationError('REPORT_CRITERIA_INVALID')
    for key in path:
        if isinstance(value, dict) and isinstance(key, str):
            value = value[key]
        elif isinstance(value, list) and type(key) is int and key >= 0:
            value = value[key]
        else:
            raise KeyError(key)
    return value


class JsonReportCriteria:
    """Compatible trusted registry parser with immutable request context seam."""
    def parse_request(self, output, exit_code, context):
        criteria = context['config'].get('report_criteria')
        if not isinstance(criteria, (list, tuple)) or not criteria:
            raise IntegrationError('REPORT_CRITERIA_REQUIRED')
        if not any('revision_path' in spec for spec in criteria):
            raise IntegrationError('REPORT_REVISION_ANCHOR_REQUIRED')
        if {spec.get('file') for spec in criteria} != set(context['config']['report_files']):
            raise IntegrationError('REPORT_CRITERIA_COVERAGE_REQUIRED')
        try:
            for spec in criteria:
                if set(spec) - {'file', 'revision_path', 'equals', 'positive', 'zero', 'each', 'at_least', 'equal_paths'}:
                    raise IntegrationError('REPORT_CRITERIA_INVALID')
                name = spec['file']
                if name not in context['config']['report_files']:
                    raise IntegrationError('REPORT_CRITERIA_FILE_NOT_DECLARED')
                report = json.loads((Path(output) / name).read_text(encoding='utf-8'))
                if 'revision_path' in spec and _select(report, spec['revision_path']) != context['request']['revision']:
                    return False
                if not self._matches(report, spec):
                    return False
            return exit_code == 0
        except (KeyError, IndexError, TypeError, ValueError):
            return False

    @classmethod
    def _matches(cls, report, spec):
        if set(spec) - {'file', 'revision_path', 'path', 'equals', 'at_least', 'positive', 'zero', 'equal_paths', 'each'}:
            raise IntegrationError('REPORT_CRITERIA_INVALID')
        for rule in spec.get('equals', ()):
            actual = _select(report, rule['path'])
            if not _equal(actual, rule['value']):
                return False
        for rule in spec.get('at_least', ()):
            actual = _select(report, rule['path'])
            minimum = rule['value']
            if type(actual) is not int or type(minimum) is not int or actual < minimum:
                return False
        for rule in spec.get('equal_paths', ()):
            left, right = _select(report, rule['left']), _select(report, rule['right'])
            if type(left) is not type(right) or left != right:
                return False
        for path in spec.get('positive', ()): 
            actual = _select(report, path)
            if type(actual) is not int or actual <= 0:
                return False
        for path in spec.get('zero', ()):
            actual = _select(report, path)
            if type(actual) is not int or actual != 0:
                return False
        for rule in spec.get('each', ()):
            collection = _select(report, rule['path'])
            if isinstance(collection, dict):
                collection = list(collection.values())
            if not isinstance(collection, list) or not collection:
                return False
            if any(not cls._matches(item, rule) for item in collection):
                return False
        return True
