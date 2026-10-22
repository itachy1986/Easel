"""Opt-in localhost smoke fixture: no credentials, model calls or publishing routes.

Build frontend, then run: python -m tests.publish_browser_fixture
Open http://127.0.0.1:8765 in the approved built-in browser. Evidence: /evidence.
Only synthetic fixtures in this checkout's .smoke-issue38 directory are used.
"""
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from web import app as web

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / '.git').is_file():
    raise RuntimeError('Run this fixture only inside an isolated Git worktree, never the formal checkout')
STATE = ROOT / '.smoke-issue38'
STATE.mkdir(exist_ok=True)
web.OUTPUTS_DIR = STATE / 'outputs'
web.OUTPUTS_DIR.mkdir(exist_ok=True)
web.ENV_FILE = STATE / 'nonexistent.env'
app = FastAPI()
events = []
network = []
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
    return {'mode': 'manual', 'automaticSubmission': False}


@app.post('/evidence')
async def evidence(request: Request):
    events.append(await request.json())
    return {'ok': True}


@app.get('/evidence')
def report():
    return {'events': events, 'network': network}


@app.get('/api/{path:path}')
def mock_get(path: str):
    if path == 'status':
        return {'gateway': 'running', 'apiDirect': True, 'modelConfigured': True}
    if path in {'personas', 'skills', 'schedules', 'sessions', 'ideas'}:
        return []
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
    script = f"""<script>
    if (!sessionStorage.getItem('smoke-stage')) {{
      localStorage.setItem('easel_sessions', {json.dumps(seed)});
      localStorage.setItem('easel_active_session', 'smoke-chat');
      localStorage.setItem('easel_onboarding_seen', '1');
      sessionStorage.setItem('easel_tab_session', 'smoke-chat');
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
