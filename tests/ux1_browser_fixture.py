"""Manual UX1 preview, synthetic data only; no application backend is imported.

Build frontend, then: python tests/ux1_browser_fixture.py
Open http://127.0.0.1:8765/?run=light&theme=light (existing draft), or
/?run=dark-new&theme=dark&new=1 (empty draft); refresh preserves edits.
Add &copyfail=1 to exercise rejected clipboard writes. /ux1 is a paste checker.
No automatic clicks, screenshots, clipboard reads, model calls or publishing.
"""
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / '.git').is_file():
    raise RuntimeError('UX1 preview must run in an isolated Git worktree')
DIST = ROOT / 'web/frontend/dist'
BLOCKS = [
    '茶园「第一张」："晨光"，' + '长行' * 100 + '\n第二行，保留标点。\n',
    '配图二：只复制这一块。\n夜景 & <灯>，画面比例 3:4。\n',
]
REPLY = ('## 标题\nUX1 合成茶园文案\n## 正文\n隔离测试，不发布。\n'
         '## 话题标签\n#茶园 #合成测试\n## Flow 提示词\n'
         '### 配图 1\n```text\n' + BLOCKS[0] + '```\n'
         '### 配图 2\n```text\n' + BLOCKS[1] + '```\n')
SESSIONS = [{'id': 'ux1-chat', 'title': 'UX1 双块复制', 'created': 1, 'messages': [
    {'role': 'user', 'content': '展示两段合成配图提示词'},
    {'role': 'assistant', 'content': REPLY},
]}]
OLD = {'version': 2, 'id': 'ux1-old', 'title': '需保留的合成旧稿', 'body': '旧正文不应被取消覆盖',
       'tags': '旧标签', 'platforms': ['xiaohongshu'], 'overrides': {}, 'media': [],
       'flowPrompt': '旧提示词', 'aspectRatio': '3:4'}
app = FastAPI()


@app.middleware('http')
async def deny_writes(request: Request, next_call):
    if request.method not in {'GET', 'HEAD'}:
        return JSONResponse({'detail': 'UX1 fixture prohibits all side effects'}, status_code=403)
    return await next_call(request)


@app.get('/api/{path:path}')
def fixture_get(path: str):
    if path == 'status':
        return {'gateway': 'running', 'apiDirect': True, 'modelConfigured': True}
    if path == 'publishing-policy':
        return {'mode': 'manual', 'automaticSubmission': False}
    if path in {'personas', 'skills', 'schedule', 'sessions', 'ideas', 'accounts', 'outputs', 'analytics/platforms'}:
        return []
    if path == 'trends':
        return {'trends': [], 'updated': 0}
    return {}


@app.get('/')
def page():
    script = f"""<script>
    const params = new URLSearchParams(location.search);
    const run = params.get('run') || 'ux1';
    if (sessionStorage.getItem('ux1-run') !== run) {{
      sessionStorage.setItem('ux1-run', run);
      localStorage.setItem('easel_sessions', JSON.stringify({json.dumps(SESSIONS, ensure_ascii=False)}));
      localStorage.setItem('easel_active_session', 'ux1-chat');
      localStorage.setItem('easel_onboarding_seen', '1');
      sessionStorage.setItem('easel_tab_session', 'ux1-chat');
      localStorage.removeItem('easel_publish_draft');
      if (!params.has('new')) localStorage.setItem('easel_publish_draft', JSON.stringify({json.dumps(OLD, ensure_ascii=False)}));
    }}
    if (['light', 'dark'].includes(params.get('theme'))) localStorage.setItem('easel_theme', params.get('theme'));
    if (params.has('copyfail')) Object.defineProperty(navigator, 'clipboard', {{
      configurable: true, value: {{writeText: async () => {{throw new Error('UX1 simulated clipboard rejection');}}}}
    }});
    window.__ux1Expected = {json.dumps(BLOCKS, ensure_ascii=False)};
    </script>"""
    return HTMLResponse((DIST / 'index.html').read_text(encoding='utf-8').replace('<head>', '<head>' + script))


@app.get('/ux1')
def paste_checker():
    return HTMLResponse('''<!doctype html><html lang="zh"><meta charset="utf-8">
    <title>UX1 真实粘贴核对</title><h1>UX1 逐块复制核对</h1>
    <p>在聊天页点击某块的“复制此块”，切换此页并粘贴。这里不会读取或写入剪贴板。</p>
    <textarea id="paste" rows="12" cols="65" aria-label="粘贴待核对文本"></textarea>
    <pre id="result" role="status">等待真实粘贴（不是已通过证据）</pre><script>
    const expected = ''' + json.dumps(BLOCKS, ensure_ascii=False) + ''';
    document.getElementById('paste').addEventListener('input', e => {
      const value = e.target.value;
      document.getElementById('result').textContent = JSON.stringify({
        block1Exact: value === expected[0], block2Exact: value === expected[1],
        length: value.length, expectedLengths: expected.map(s => s.length), actual: value
      }, null, 2);
    });</script></html>''')


app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8765)
