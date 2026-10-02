"""Container-only Playwright/axe runner. Flow is data, never Python or shell."""
import importlib.metadata
import json
from pathlib import Path
import sys
from urllib.parse import urljoin, urlsplit

from common import PLAYWRIGHT_VERSION, atomic_json, sha256, summarize


def validate_flow(flow):
    if not isinstance(flow, dict) or set(flow) - {'spec_reference', 'author', 'viewports', 'scenarios'}:
        raise ValueError('FLOW_INVALID')
    if not flow.get('spec_reference') or not flow.get('author'):
        raise ValueError('SPEC_AUTHOR_REQUIRED')
    views, scenarios = flow.get('viewports'), flow.get('scenarios')
    if not isinstance(views, list) or not views or not isinstance(scenarios, list) or not scenarios:
        raise ValueError('EMPTY_FLOW')
    for view in views:
        if set(view) != {'width', 'height'} or any(type(v) is not int or v <= 0 for v in view.values()):
            raise ValueError('VIEWPORT_INVALID')
    ids = set()
    for scenario in scenarios:
        if set(scenario) != {'id', 'path', 'steps'} or not isinstance(scenario['id'], str) or scenario['id'] in ids:
            raise ValueError('SCENARIO_INVALID')
        ids.add(scenario['id'])
        path = scenario['path']
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or urlsplit(path).scheme:
            raise ValueError('LOCAL_ROUTE_REQUIRED')
        if not isinstance(scenario['steps'], list) or not scenario['steps']:
            raise ValueError('EMPTY_SCENARIO')
        for step in scenario['steps']:
            if not isinstance(step, dict) or set(step) - {'action', 'selector', 'value'}:
                raise ValueError('STEP_INVALID')
            if step.get('action') not in {'click', 'fill', 'press', 'visible', 'text', 'focused', 'integer', 'integer_delta', 'reload'} or not isinstance(step.get('selector'), str):
                raise ValueError('STEP_INVALID')
            if step['action'] in {'fill', 'press', 'text', 'integer_delta'} and not isinstance(step.get('value'), str):
                raise ValueError('STEP_VALUE_REQUIRED')
    return flow


def run(flow_path, axe_path, scratch, base_url):
    from playwright.sync_api import sync_playwright, expect
    version = importlib.metadata.version('playwright')
    if version != PLAYWRIGHT_VERSION:
        raise ValueError('PLAYWRIGHT_PIN_MISMATCH')
    scratch = Path(scratch)
    flow = validate_flow(json.loads(Path(flow_path).read_text(encoding='utf-8')))
    checks, findings, artifacts = [], [], []
    tools = {'playwright': version, 'timeouts': 'Unmodified Playwright API defaults: actions/navigation 30000ms; expect 5000ms; no checker or broker deadline'}
    def artifact(path, kind):
        artifacts.append({'kind': kind, 'file': path.name, 'sha256': sha256(path)})
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        tools['chromium'] = browser.version
        try:
            for vi, view in enumerate(flow['viewports']):
                for si, scenario in enumerate(flow['scenarios']):
                    prefix = f'v{vi}-s{si}'
                    context = browser.new_context(viewport=view)
                    context.tracing.start(screenshots=True, snapshots=True, sources=False)
                    page = context.new_page()
                    page_errors, console_errors, response_errors, request_errors = [], [], [], []
                    page.on('pageerror', lambda e: page_errors.append(str(e)))
                    page.on('console', lambda e: console_errors.append(e.text) if e.type == 'error' else None)
                    page.on('response', lambda r: response_errors.append({'url': r.url, 'status': r.status}) if r.status >= 400 else None)
                    page.on('requestfailed', lambda r: request_errors.append({'url': r.url, 'failure': r.failure}))
                    origin = urlsplit(base_url)
                    def route_handler(route):
                        target = urlsplit(route.request.url)
                        if target.scheme in {'data', 'blob', 'about'} or (target.scheme, target.netloc) == (origin.scheme, origin.netloc):
                            route.continue_()
                        else:
                            route.abort()
                    context.route('**/*', route_handler)
                    try:
                        page.goto(urljoin(base_url, scenario['path']), wait_until='load')
                        import re
                        integer_baselines = {}
                        for step in scenario['steps']:
                            loc = page.locator(step['selector'])
                            action = step['action']
                            if action == 'click': loc.click()
                            elif action == 'fill': loc.fill(step['value'])
                            elif action == 'press': loc.press(step['value'])
                            elif action == 'visible': expect(loc).to_be_visible()
                            elif action == 'focused': expect(loc).to_be_focused()
                            elif action == 'text': expect(loc).to_have_text(step['value'])
                            elif action == 'integer':
                                expect(loc).to_have_text(re.compile(r'^-?\d+$'))
                                integer_baselines[step['selector']] = int(loc.inner_text())
                            elif action == 'integer_delta':
                                expect(loc).to_have_text(str(integer_baselines[step['selector']] + int(step['value'])))
                            elif action == 'reload': page.reload(wait_until='load')
                        checks.append({'id': prefix + '-flow', 'status': 'PASS', 'scenario': scenario['id'], 'viewport': view})
                    except AssertionError:
                        checks.append({'id': prefix + '-flow', 'status': 'FAIL', 'scenario': scenario['id'], 'viewport': view})
                        findings.append({'check': prefix + '-flow', 'kind': 'spec_assertion', 'candidate': True})
                    # Operational browser/navigation failures propagate as ERROR, not product failure.
                    try:
                        overflow = page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth')
                        checks.append({'id': prefix + '-overflow', 'status': 'FAIL' if overflow else 'PASS', 'viewport': view})
                        if overflow:
                            findings.append({'check': prefix + '-overflow', 'kind': 'document_horizontal_overflow', 'candidate': True, 'review_note': 'Check spec and intentional scroll regions before accepting a defect.'})
                        page.add_script_tag(path=str(axe_path))
                        tools['axe_runtime'] = page.evaluate('axe.version')
                        axe = page.evaluate('async () => await axe.run(document)')
                        raw = scratch / (prefix + '-axe.json')
                        atomic_json(raw, axe)
                        artifact(raw, 'private-axe-raw')
                        checks.append({'id': prefix + '-axe', 'status': 'FAIL' if axe['violations'] else 'PARTIAL' if axe['incomplete'] else 'PASS', 'viewport': view})
                        for group in ('violations', 'incomplete'):
                            for item in axe[group]:
                                findings.append({'check': prefix + '-axe', 'kind': group, 'rule': item['id'], 'impact': item.get('impact'), 'tags': item.get('tags', []), 'candidate': True, 'nodes': len(item['nodes'])})
                        logs = scratch / (prefix + '-browser.json')
                        atomic_json(logs, {'page_errors': page_errors, 'console_errors': console_errors, 'response_errors': response_errors, 'request_errors': request_errors})
                        artifact(logs, 'private-browser-raw')
                        bad = any((page_errors, console_errors, response_errors, request_errors))
                        checks.append({'id': prefix + '-browser-errors', 'status': 'FAIL' if bad else 'PASS', 'viewport': view})
                        if bad: findings.append({'check': prefix + '-browser-errors', 'kind': 'browser_error_candidate', 'candidate': True})
                        image = scratch / (prefix + '.png')
                        page.screenshot(path=str(image), full_page=True)
                        artifact(image, 'screenshot-not-visually-reviewed')
                    finally:
                        trace = scratch / (prefix + '-trace.zip')
                        context.tracing.stop(path=str(trace))
                        artifact(trace, 'private-trace')
                        context.close()
        finally:
            browser.close()
    report = {'execution': 'COMPLETED', 'checks': checks, 'summary': summarize(checks), 'findings': findings,
              'artifacts': artifacts, 'viewports': flow['viewports'], 'tools': tools,
              'visual_review': 'NOT_PERFORMED', 'spec_reference': flow['spec_reference'], 'flow_author': flow['author']}
    atomic_json(scratch / 'candidate.json', report)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(run(*sys.argv[1:]))
    except Exception:
        # Safe error code only. Raw browser data stays in private scratch.
        atomic_json(Path(sys.argv[3]) / 'candidate.json', {'execution': 'ERROR', 'checks': [{'id': 'browser-execution', 'status': 'ERROR'}], 'findings': [], 'artifacts': [], 'error': 'BROWSER_OR_FLOW_ERROR'})
        sys.exit(1)
