import { useEffect, useRef, useState } from 'react';
import { hasPublishContent, importPublishDraft, loadPublishDraft } from '../lib/store';
import { parsePublishReply } from '../lib/publishHandoff';

export default function PublishHandoff({ text, sourceId, onClose, onImported }: {
  text: string; sourceId: string; onClose: () => void; onImported: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState(() => parsePublishReply(text, 'xiaohongshu', sourceId));
  const [error, setError] = useState('');
  const existing = hasPublishContent(loadPublishDraft());
  useEffect(() => { dialog.current?.showModal(); }, []);
  const accept = (choice: 'new' | 'replace') => {
    try { importPublishDraft(draft, choice); onImported(); }
    catch { setError('草稿保存失败，原稿仍保留。请检查浏览器存储空间后重试。'); }
  };
  return <dialog ref={dialog} className="publish-handoff" onCancel={onClose} aria-labelledby="handoff-title">
    <h2 id="handoff-title">发送到发布中心</h2>
    <p className="pv-hint">核对可见回复，字段不明确时留空。这里仅保存草稿。</p>
    <label className="field-label" htmlFor="handoff-platform">目标平台</label>
    <select id="handoff-platform" className="field" value={draft.platforms[0]} onChange={e => {
      const reparsed = parsePublishReply(text, e.target.value, sourceId);
      setDraft({ ...reparsed, id: draft.id });
    }}>
      <option value="xiaohongshu">小红书</option><option value="wechat-oa">公众号</option>
    </select>
    <label className="field-label" htmlFor="handoff-name">标题</label>
    <input id="handoff-name" className="field" value={draft.title} onChange={e => setDraft({ ...draft, title: e.target.value })} />
    <label className="field-label" htmlFor="handoff-body">发布正文</label>
    <textarea id="handoff-body" className="field" rows={6} value={draft.body} onChange={e => setDraft({ ...draft, body: e.target.value })} />
    <label className="field-label" htmlFor="handoff-tags">话题标签</label>
    <input id="handoff-tags" className="field" value={draft.tags} onChange={e => setDraft({ ...draft, tags: e.target.value })} />
    <p className="pv-hint">{draft.flowPrompt ? '独立 Flow 提示词将随草稿保存，可在发布中心编辑。' : '尚无 Flow 提示词，可在发布中心填写。'}</p>
    {draft.parseWarnings?.map(warning => <p key={warning} role="alert">{warning}</p>)}
    <details><summary>核对回复原文</summary><pre style={{ whiteSpace: 'pre-wrap' }}>{text}</pre></details>
    {existing && <p role="status">已有未完成草稿：可保留旧稿另存，或明确覆盖当前草稿。</p>}
    {error && <p role="alert">{error}</p>}
    <div className="msg-actions">
      <button className="btn" onClick={onClose}>取消</button>
      {existing && <button className="btn" onClick={() => accept('replace')}>覆盖当前草稿</button>}
      <button className="btn btn-primary" onClick={() => accept('new')}>{existing ? '保留旧稿，另存新稿' : '保存草稿并前往'}</button>
    </div>
  </dialog>;
}
