import { useEffect, useRef, useState } from 'react';
import { FLOW_URL } from '../lib/publishHandoff';
import { loadPublishDraft } from '../lib/store';
import type { PublishDraft } from '../lib/store';
import { importDraftImages, mediaUrl } from '../lib/api';

export default function FlowDraftPanel({ draft, onPrompt, onRatio, onMedia, onPick }: {
  draft: PublishDraft; onPrompt: (text: string) => void; onRatio: (ratio: string) => void;
  onMedia: (update: (old: string[]) => string[]) => void; onPick: () => void;
}) {
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [missing, setMissing] = useState<string[]>([]);
  const [broken, setBroken] = useState<string[]>([]);
  const upload = useRef<AbortController | null>(null);
  useEffect(() => () => upload.current?.abort(), []);
  useEffect(() => {
    let active = true;
    Promise.all((draft.media || []).map(async path => {
      try {
        const response = await fetch(mediaUrl(path));
        await response.body?.cancel();
        return response.ok ? '' : path;
      } catch { return path; }
    })).then(paths => { if (active) setMissing(paths.filter(Boolean)); });
    return () => { active = false; };
  }, [draft.media]);
  useEffect(() => setBroken(old => old.filter(path => draft.media?.includes(path))), [draft.media]);
  const receive = async (files: File[]) => {
    if (!files.length || busy || !draft.id) return;
    const id = draft.id;
    const controller = new AbortController(); upload.current = controller;
    setBusy(true); setNotice('');
    try {
      const paths = await importDraftImages(files, id, controller.signal);
      if (!controller.signal.aborted && loadPublishDraft().id === id) {
        onMedia(old => [...new Set([...old, ...paths])]); setNotice('图片已加入当前草稿，文案与标签保持不变。');
      }
    } catch (error) { if (!controller.signal.aborted) setNotice(error instanceof Error ? error.message : '上传失败，请重试。'); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  };
  return <section className="panel" aria-label="Flow 配图与草稿素材">
    <h2>配图提示词（Nano Banana / Google Flow）</h2>
    <label className="field-label" htmlFor="flow-ratio">画面比例</label>
    <input id="flow-ratio" className="field" value={draft.aspectRatio || '3:4'} onChange={e => onRatio(e.target.value)} />
    <label className="field-label" htmlFor="flow-prompt">独立配图提示词</label>
    <textarea id="flow-prompt" className="field" rows={4} value={draft.flowPrompt || ''} onChange={e => onPrompt(e.target.value)} placeholder="请让聊天助手按正文提供主体、场景、构图、光线、风格与留白，或自行填写。" />
    <div className="msg-actions">
      <button className="btn btn-sm" disabled={!draft.flowPrompt?.trim()} onClick={async () => {
        try { await navigator.clipboard.writeText(`画面比例：${draft.aspectRatio || '3:4'}\n${draft.flowPrompt}`); setNotice('提示词已复制'); }
        catch { setNotice('复制失败，请选择提示词手动复制。'); }
      }}>复制 Flow 提示词</button>
      <a className="btn btn-sm" href={FLOW_URL} target="_blank" rel="noopener noreferrer">前往 Google Flow</a>
    </div>
    <p className="pv-hint">在 Flow 选择 Image / Nano Banana，自行生成并下载，再上传到当前草稿。</p>
    <label className="field-label" htmlFor="flow-upload">上传生成图片</label>
    <input id="flow-upload" type="file" accept="image/png,image/jpeg,image/webp" multiple disabled={busy} onChange={e => { void receive(Array.from(e.target.files || [])); e.target.value = ''; }} />
    <button className="btn btn-sm" onClick={onPick}>从内容库选择</button>
    {busy && <button className="btn btn-sm" onClick={() => { upload.current?.abort(); setBusy(false); setNotice('已取消回填，草稿未更改。'); }}>取消上传</button>}
    {!!draft.media?.length && <div className="media-grid" aria-label="当前草稿素材预览">
      {draft.media.map(path => /\.(?:png|jpe?g|gif|webp|bmp|avif)$/i.test(path)
        ? <div className="media-cell sel" key={path}>
          <img data-draft-media src={mediaUrl(path)} alt={`当前草稿图片 ${path.split('/').pop() || ''}`}
            onError={() => setBroken(old => old.includes(path) ? old : [...old, path])} />
        </div>
        : <div className="media-cell sel" key={path}><span className="media-vid">视频 · {path.split('/').pop()}</span></div>)}
    </div>}
    {broken.length > 0 && <p role="alert">部分图片无法解码显示，请移除后重新上传 PNG/JPEG/WebP，或从内容库重新选择。</p>}
    <p role="status">{missing.length ? '部分素材已失效，请移除后重新上传或从内容库选择。' : !draft.media?.length ? '缺少图片/视频：请上传生成图片或从内容库选择。' : '已具备图片/视频，请核对预览后再审核。'}</p>
    {notice && <p role="status">{notice}</p>}
    {draft.originalResponse && <details><summary>核对聊天原文</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{draft.originalResponse}</pre></details>}
  </section>;
}
