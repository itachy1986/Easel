import {
  DEVICE_CODE_COMMAND,
  isExclusivePlanUsable,
  useChatGPTPlan,
} from './useChatGPTPlan';
import type { ChatGPTPlanSummary } from './useChatGPTPlan';

export type { ChatGPTPlanSummary } from './useChatGPTPlan';

interface Props {
  onSummaryChange?: (summary: ChatGPTPlanSummary) => void;
}

export default function ChatGPTPlanCard({ onSummaryChange }: Props) {
  const plan = useChatGPTPlan(onSummaryChange);
  const {
    status, catalog, job, selectedModel, setSelectedModel, loading, catalogLoading,
    testing, message, messageKind, copyNote, openAIModels, busy, canTest,
    startConnect, cancelJob, refreshCatalog, testAndUse, copyDeviceCommand,
  } = plan;

  const riskCopy = (() => {
    if (!status) return '';
    if (status.billingSource === 'mixed' || status.errorCode === 'platform_fallback_present') {
      return '检测到 ChatGPT Plan 与 OpenAI Platform credential 同时存在。为避免意外 API 计费，Test & use 已禁用。';
    }
    if (status.billingSource === 'platform_api') {
      return '检测到 OpenAI Platform credential。你仍可登录 ChatGPT Plan；在 Platform 计费来源未被安全隔离前，Easel 不会启用 Test & use。';
    }
    if (status.errorCode === 'billing_ambiguity'
      || (status.connected && status.billingSource === 'unknown')) {
      return '无法确认唯一订阅计费来源，请刷新并检查 OpenClaw；Test & use 已安全禁用。';
    }
    return '';
  })();

  const statusView = (() => {
    if (loading) return { cls: 'off', label: '检查中' };
    if (isExclusivePlanUsable(status)) return { cls: 'ok', label: 'Connected' };
    if (status?.reauthRequired) return { cls: 'warn', label: '需要重新登录' };
    if (status?.billingSource === 'platform_api') return { cls: 'warn', label: 'Platform API' };
    if (status?.connected) return { cls: 'warn', label: '需检查' };
    if (status?.available === false) return { cls: 'fail', label: '状态不可用' };
    return { cls: 'off', label: '未登录' };
  })();

  const catalogCopy = (() => {
    if (catalogLoading) return '正在读取动态模型目录…';
    if (catalog?.status === 'empty') return '当前账号未返回可用模型。';
    if (catalog?.status === 'stale') return '模型目录已过期，请刷新。';
    if (catalog?.status === 'unavailable') return '模型目录暂不可用，请稍后重试。';
    return '';
  })();

  const canStartAuth = Boolean(
    status?.available
    && !busy
    && (status.reauthRequired || !status.connected),
  );

  const connectingCopy = (() => {
    if (job?.phase === 'gateway_starting') return '正在启动本机 OpenClaw Gateway…';
    if (job?.phase === 'gateway_connecting') return 'Gateway 已启动，正在建立安全登录连接…';
    if (job?.phase === 'browser_opened') return '已打开安全登录页面；请在浏览器完成登录后返回 Easel。';
    return '正在等待 OpenClaw 完成登录…';
  })();

  return (
    <section className="chatgpt-plan" data-testid="chatgpt-plan-card" aria-labelledby="chatgpt-plan-title">
      <div className="chatgpt-plan-head">
        <div>
          <h3 id="chatgpt-plan-title">ChatGPT Plan</h3>
          <p>通过 OpenClaw 使用支持的 ChatGPT 订阅，不需要在 Easel 填 API Key。</p>
        </div>
        <span className={`pill ${statusView.cls}`}><span className="dot" />{statusView.label}</span>
      </div>

      <div className="chatgpt-plan-body">
        <div className="chatgpt-plan-status" data-testid="plan-status-copy" aria-live="polite">
          {status && isExclusivePlanUsable(status)
            ? <><strong>{status.displayLabel || 'ChatGPT Plan account'}</strong><span>由 OpenClaw 管理</span></>
            : status?.reauthRequired
              ? <span>需要重新登录后才能读取模型并安全验证。</span>
              : status?.connected
                ? <span>ChatGPT Plan 已登录 · 尚未安全启用</span>
                : <span>ChatGPT Plan 与 API Key 通道分开管理。</span>}
        </div>

        {riskCopy ? <div className="plan-notice risk" data-testid="plan-risk" role="alert">{riskCopy}</div> : null}

        {busy ? (
          <div className="plan-connect" data-testid="plan-connecting" role="status">
            <span className="spin" />
            <span>{connectingCopy}</span>
            <button className="btn btn-sm" onClick={() => void cancelJob()}>取消登录</button>
          </div>
        ) : null}

        {canStartAuth ? (
          <button className="btn btn-primary" onClick={() => void startConnect('siwc')}>
            {status?.reauthRequired ? '重新登录 ChatGPT' : 'Continue with ChatGPT (Beta)'}
          </button>
        ) : null}

        {status && isExclusivePlanUsable(status) ? (
          <div className="plan-model-row">
            <label htmlFor="chatgpt-plan-model">订阅可用模型</label>
            {catalog?.status === 'available' ? (
              <select id="chatgpt-plan-model" data-testid="plan-model-select" value={selectedModel} onChange={(event) => setSelectedModel(event.target.value)}>
                {openAIModels.map((model) => (
                  <option key={model.ref} value={model.ref} disabled={model.availability !== 'available'}>
                    {model.displayName || model.name || model.ref}{model.availability === 'available' ? '' : '（不可用）'}
                  </option>
                ))}
              </select>
            ) : null}
            {catalogCopy ? <span data-testid="plan-catalog-state" className="plan-catalog-state">{catalogCopy}</span> : null}
            {catalog?.status === 'available' ? (
              <button className="btn btn-sm btn-primary" onClick={() => void testAndUse()} disabled={!canTest}>
                {testing ? '验证中…' : 'Test & use'}
              </button>
            ) : (
              <button className="btn btn-sm" onClick={() => void refreshCatalog()} disabled={catalogLoading}>刷新模型</button>
            )}
          </div>
        ) : null}

        {message ? <div className={`plan-notice ${messageKind}`} data-testid="plan-message" role="status">{message}</div> : null}

        <details className="plan-compat">
          <summary>兼容登录方式</summary>
          <div className="plan-compat-row">
            <div>
              <strong>OpenClaw-managed OAuth</strong>
              <p>由 OpenClaw 打开浏览器授权；不会读取或复用 Codex CLI 私有 credential store。</p>
            </div>
            <button className="btn btn-sm" onClick={() => void startConnect('oauth')} disabled={!canStartAuth}>使用 OAuth 登录</button>
          </div>
          <div className="plan-compat-row">
            <div>
              <strong>Device code · terminal-only</strong>
              <p>请在本机终端运行固定命令；Easel 不读取终端输出。</p>
            </div>
            <div className="plan-command">
              <input data-testid="device-code-command" value={DEVICE_CODE_COMMAND} readOnly aria-label="Device code terminal command" />
              <button className="btn btn-sm" onClick={() => void copyDeviceCommand()}>复制命令</button>
              {copyNote ? <span>{copyNote}</span> : null}
            </div>
          </div>
        </details>

        <p className="plan-separation">ChatGPT Plan 登录由 OpenClaw 管理，不读取 Codex CLI 的私有凭据；“本机 Agent”是另一种接入方式。</p>
      </div>
    </section>
  );
}
