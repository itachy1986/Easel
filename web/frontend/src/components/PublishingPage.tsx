import { useEffect, useState } from 'react';
import { fetchPublishingPolicy } from '../lib/api';
import type { PublishingPolicy } from '../lib/api';
import AutomatedPublishPage from './PublishPage';
import ManualPublishPage from './ManualPublishPage';
import { listPublishDrafts, loadPublishDraft, selectPublishDraft } from '../lib/store';

export default function PublishingPage({ persona }: { persona: string }) {
  const [draftId, setDraftId] = useState(() => loadPublishDraft().id);
  const [draftError, setDraftError] = useState('');
  const [policy, setPolicy] = useState<PublishingPolicy | null>(null);
  const [mode, setMode] = useState<PublishingPolicy['mode']>('automated');
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setError('');
    fetchPublishingPolicy().then((value) => {
      if (active) {
        setPolicy(value);
        setMode(value.mode === 'automated' && value.automaticSubmission ? 'automated' : 'manual');
      }
    })
      .catch(() => { if (active) setError('无法读取发布方式，请重试。'); });
    return () => { active = false; };
  }, [retry]);
  if (error) return <div className="panel" role="alert">{error}
    <button className="btn btn-sm" onClick={() => setRetry((value) => value + 1)}>重新读取</button>
  </div>;
  if (!policy) return <div className="dash-empty">正在读取发布方式…</div>;
  const automaticAllowed = policy.mode === 'automated' && policy.automaticSubmission;
  const manual = mode === 'manual' || !automaticAllowed;
  return <div className="publishing-workspace">
    <label className="field-label">当前发布草稿
      <select className="field" value={draftId} onChange={e => {
        try { selectPublishDraft(e.target.value); setDraftId(e.target.value); setDraftError(''); }
        catch { setDraftError('草稿切换失败，当前草稿已保留。'); }
      }}>{listPublishDrafts().map(d => <option key={d.id} value={d.id}>{d.title || '未命名草稿'}</option>)}</select>
    </label>
    {draftError && <p role="alert">{draftError}</p>}
    <div className="publishing-mode-bar">
      <div className="subnav-tabs" role="group" aria-label="发布方式">
        <button type="button" className={`subnav-tab ${!manual ? 'active' : ''}`}
          aria-pressed={!manual} disabled={!automaticAllowed} onClick={() => setMode('automated')}>
          自动发布
        </button>
        <button type="button" className={`subnav-tab ${manual ? 'active' : ''}`}
          aria-pressed={manual} onClick={() => setMode('manual')}>
          人工审核发布
        </button>
      </div>
      <span className="pv-hint">{!automaticAllowed
        ? '管理员已禁用自动发布，当前仅提供人工审核路径。'
        : manual ? '审核后复制或导出，前往官方页面亲自提交；切换不更改全局自动发布权限。'
          : '保留原有账号检查、预检、确认及自动提交流程。'}</span>
    </div>
    <div className="publishing-mode-content">
      {manual ? <ManualPublishPage key={draftId} persona={persona} /> : <AutomatedPublishPage key={draftId} persona={persona} />}
    </div>
  </div>;
}
