"""Opt-in localhost smoke fixture: no credentials, model calls or publishing routes.

Build frontend, then run: python -m tests.publish_browser_fixture
Open http://127.0.0.1:8765 in the approved built-in browser. Evidence: /evidence.
Only synthetic fixtures in this checkout's .smoke-issue38 directory are used.
"""
import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from web import app as web

ROOT = Path(__file__).resolve().parents[1]
is_isolated_worktree = (ROOT / '.git').is_file()
is_github_actions_runner = os.environ.get('GITHUB_ACTIONS') == 'true'
if not (is_isolated_worktree or is_github_actions_runner):
    raise RuntimeError('Run this fixture only inside an isolated Git worktree, never the formal checkout')
STATE = ROOT / '.smoke-issue38'
STATE.mkdir(exist_ok=True)
web.OUTPUTS_DIR = STATE / 'outputs'
web.OUTPUTS_DIR.mkdir(exist_ok=True)
web.ENV_FILE = STATE / 'nonexistent.env'
app = FastAPI()
events = []
network = []
policy_mode = 'manual'
run_id = ''
required_steps = {'chat-to-draft', 'image-upload', 'refresh-preview', 'manual-export', 'automatic-preview'}
allowed = {'/api/publish-draft/images', '/api/manual-publish/{platform}/export', '/api/media/{path:path}', '/api/outputs'}
for route in web.app.routes:
    if getattr(route, 'path', '') in allowed:
        app.router.routes.append(route)


@app.middleware('http')
async def record(request, next_call):
    network.append(f'{request.method} {request.url.path}')
    return await next_call(request)


@app.get('/api/publishing-policy')
def policy():
    return {'mode': policy_mode, 'automaticSubmission': policy_mode == 'automated'}


@app.post('/fixture/policy/{mode}')
def set_policy(mode: str):
    global policy_mode
    if mode not in {'manual', 'automated'}:
        return JSONResponse({'detail': 'invalid fixture mode'}, status_code=400)
    policy_mode = mode
    return {'ok': True, 'mode': mode}


@app.post('/fixture/run')
def start_run():
    global policy_mode, run_id
    events.clear()
    network.clear()
    policy_mode = 'manual'
    run_id = uuid.uuid4().hex[:12]
    return {'ok': True, 'runId': run_id, 'mode': policy_mode}


@app.get('/fixture/assertions')
def assertions():
    forbidden = [entry for entry in network if entry.startswith('POST /api/publish/') or entry == 'POST /api/chat']
    return {'ok': not forbidden, 'forbidden': forbidden, 'runId': run_id}


@app.get('/fixture/result')
def result():
    succeeded = {event.get('step') for event in events if event.get('ok') is True}
    failures = [event for event in events if event.get('ok') is not True]
    boundary = assertions()
    missing = sorted(required_steps - succeeded)
    return {
        'ok': bool(run_id) and not missing and not failures and boundary['ok'],
        'runId': run_id, 'missing': missing, 'failures': failures,
        'forbidden': boundary['forbidden'],
    }


@app.post('/evidence')
async def evidence(request: Request):
    payload = await request.json()
    if (not isinstance(payload, dict) or not isinstance(payload.get('step'), str)
            or not isinstance(payload.get('ok'), bool)
            or ('detail' in payload and not isinstance(payload['detail'], str))):
        return JSONResponse({'detail': 'invalid evidence event'}, status_code=400)
    events.append({**payload, 'runId': run_id})
    return {'ok': True}


@app.get('/evidence')
def report():
    return {'runId': run_id, 'events': events, 'network': network, 'result': result()}


@app.get('/api/{path:path}')
def mock_get(path: str):
    if path == 'status':
        return {'gateway': 'running', 'apiDirect': True, 'modelConfigured': True}
    if path in {'personas', 'skills', 'schedule', 'sessions', 'ideas', 'accounts', 'analytics/platforms'}:
        return []
    if path == 'trends':
        return {'trends': [], 'updated': 0}
    return {}


@app.post('/api/{path:path}')
def prohibit_side_effects(path: str):
    return JSONResponse({'detail': 'Smoke fixture: model/publish calls prohibited'}, status_code=403)


@app.get('/')
def page():
    seed = json.dumps([{'id': 'smoke-chat', 'title': '茶园测试', 'created': 1, 'messages': [
        {'role': 'user', 'content': '写一篇小红书茶园文案'},
        {'role': 'assistant', 'content': '## 标题\n春日茶园\n## 正文\n在茶园散步。\n## 话题标签\n#春天 #茶园\n## Nano Banana 生图提示词\n茶园晨光，绿色，三分构图，顶部留白。',
         'thinking': 'INTERNAL_NOT_FOR_PUBLISH', 'activity': 'TOOL_NOT_FOR_PUBLISH'},
    ]}], ensure_ascii=False)
    old_draft = json.dumps({
        'version': 2, 'id': 'smoke-old-draft', 'title': '旧稿', 'body': '保留的旧正文',
        'tags': '', 'platforms': ['xiaohongshu'], 'overrides': {}, 'media': [],
        'flowPrompt': '', 'aspectRatio': '3:4',
    }, ensure_ascii=False)
    script = f"""<script>
    const requestedRun = new URLSearchParams(location.search).get('run') || 'default';
    if (sessionStorage.getItem('smoke-run-request') !== requestedRun) {{
      sessionStorage.setItem('smoke-run-request', requestedRun);
      sessionStorage.removeItem('smoke-stage');
      sessionStorage.removeItem('smoke-run-id');
    }}
    if (!sessionStorage.getItem('smoke-stage')) {{
      localStorage.setItem('easel_sessions', {json.dumps(seed)});
      localStorage.setItem('easel_active_session', 'smoke-chat');
      localStorage.setItem('easel_onboarding_seen', '1');
      sessionStorage.setItem('easel_tab_session', 'smoke-chat');
      localStorage.setItem('easel_publish_draft', {json.dumps(old_draft)});
    }}
    </script><script defer src='/smoke.js'></script>"""
    return HTMLResponse((ROOT / 'web/frontend/dist/index.html').read_text(encoding='utf-8').replace('<head>', '<head>' + script))


@app.get('/smoke.js')
def smoke_script():
    from fastapi.responses import Response
    return Response((ROOT / 'tests/publish_browser_smoke.js').read_text(encoding='utf-8'), media_type='text/javascript')


app.mount('/assets', StaticFiles(directory=ROOT / 'web/frontend/dist/assets'), name='assets')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8765)
