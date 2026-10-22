import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


fixture_path = Path(__file__).with_name('publish_browser_fixture.py')
fixture_spec = importlib.util.spec_from_file_location('issue38_publish_browser_fixture', fixture_path)
assert fixture_spec is not None and fixture_spec.loader is not None
fixture = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixture)


client = TestClient(fixture.app)


def start_run():
    response = client.post('/fixture/run')
    assert response.status_code == 200
    return response.json()['runId']


def test_dashboard_empty_data_matches_real_api_contracts():
    start_run()
    assert client.get('/api/schedule').json() == []
    assert client.get('/api/accounts').json() == []
    assert client.get('/api/analytics/platforms').json() == []
    assert client.get('/api/trends').json() == {'trends': [], 'updated': 0}


def test_a_new_run_discards_old_failures_and_requires_all_current_steps():
    old_run = start_run()
    assert client.post('/evidence', json={'step': 'failure', 'ok': False, 'detail': 'old'}).status_code == 200
    new_run = start_run()
    assert new_run != old_run
    assert client.get('/evidence').json()['events'] == []
    assert client.get('/fixture/result').json()['ok'] is False

    required = ['chat-to-draft', 'image-upload', 'refresh-preview', 'manual-export', 'automatic-preview']
    for step in required:
        assert client.post('/evidence', json={'step': step, 'ok': True}).status_code == 200
    result = client.get('/fixture/result').json()
    assert result['ok'] is True
    assert result['runId'] == new_run
    assert result['missing'] == []


def test_any_current_failure_prevents_pass_even_when_all_steps_exist():
    start_run()
    for step in ['chat-to-draft', 'image-upload', 'refresh-preview', 'manual-export', 'automatic-preview']:
        client.post('/evidence', json={'step': step, 'ok': True})
    client.post('/evidence', json={'step': 'failure', 'ok': False, 'detail': 'same run'}).raise_for_status()
    assert client.get('/fixture/result').json()['ok'] is False


def test_evidence_rejects_malformed_or_non_boolean_results():
    start_run()
    assert client.post('/evidence', json=[]).status_code == 400
    assert client.post('/evidence', json={'step': 'chat-to-draft', 'ok': 'true'}).status_code == 400
    assert client.get('/evidence').json()['events'] == []


def test_rerun_page_seeds_old_draft_and_explicit_run_identity():
    page = client.get('/?run=exact-head-2').text
    assert 'smoke-run-request' in page
    assert 'easel_publish_draft' in page
    assert 'smoke-old-draft' in page
