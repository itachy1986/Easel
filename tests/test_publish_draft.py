import io
import zipfile
import pytest
from fastapi.testclient import TestClient
from web import app as web
from easel.persona import chat_turn_message


def test_social_prompt_only_for_explicit_creation():
    assert 'Nano Banana' in chat_turn_message('请写一篇小红书茶园图文文案', None)
    assert 'Nano Banana' in chat_turn_message('把这篇文章改写为公众号文章', None)
    assert 'Nano Banana' not in chat_turn_message('小红书为什么登录失败？', None)
    assert 'Nano Banana' not in chat_turn_message('修复公众号发布按钮 bug', None)
    assert 'Nano Banana' not in chat_turn_message('今天天气怎么样', None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'OUTPUTS_DIR', tmp_path)
    monkeypatch.setattr(web, 'ENV_FILE', tmp_path / '.env')
    return TestClient(web.app, base_url='http://localhost:7860', headers={'Origin': 'http://localhost:7860'})


def test_import_image_can_export_without_relaxing_private_paths(client, tmp_path):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (16, 16), 'green').save(buffer, format='PNG')
    image = buffer.getvalue()
    response = client.post('/api/publish-draft/images', data={'draftId': 'draft-a'}, files={'files': ('cover.png', image, 'image/png')})
    assert response.status_code == 200, response.text
    path = response.json()['media'][0]
    assert path.startswith('draft-media/')
    assert (tmp_path / path).read_bytes() == image
    assert list((tmp_path / '_inbox').rglob('*.png')), 'original retained'
    exported = client.post('/api/manual-publish/xiaohongshu/export', json={'title': '茶园', 'body': '原文', 'tags': '春天', 'media': [path], 'reviewed': True})
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(tmp_path / exported.json()['path']) as package:
        assert package.read('body.txt').decode() == '原文'
        assert any(n.startswith('media/') for n in package.namelist())
    private = list((tmp_path / '_inbox').rglob('*.png'))[0].relative_to(tmp_path).as_posix()
    rejected = client.post('/api/manual-publish/xiaohongshu/export', json={'title': '茶园', 'media': [private], 'reviewed': True})
    assert rejected.status_code == 400


def test_invalid_image_and_draft_id_rejected(client):
    for identifier, filename in [('draft-a', 'bad.png'), ('../escape', 'cover.png')]:
        response = client.post('/api/publish-draft/images', data={'draftId': identifier}, files={'files': (filename, b'not an image', 'image/png')})
        assert response.status_code == 400


def test_mixed_batch_is_not_partially_imported(client, tmp_path):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8)).save(buffer, format='PNG')
    response = client.post('/api/publish-draft/images', data={'draftId': 'draft-a'}, files=[
        ('files', ('good.png', buffer.getvalue(), 'image/png')),
        ('files', ('bad.png', b'invalid', 'image/png')),
    ])
    assert response.status_code == 400
    assert not list((tmp_path / 'draft-media').rglob('*'))
    assert len(list((tmp_path / '_inbox').rglob('*.png'))) == 2


def test_direct_api_gets_same_selective_instruction():
    content, system = web._direct_chat_content(web.ChatRequest(message='写一篇小红书图文文案'))
    assert content == '写一篇小红书图文文案'
    assert 'Nano Banana' in system
    _, ordinary = web._direct_chat_content(web.ChatRequest(message='小红书登录失败怎么办'))
    assert '〔社媒草稿格式' not in ordinary
