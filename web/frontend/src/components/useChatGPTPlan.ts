import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  cancelOpenAIPlanJob,
  connectOpenAIPlan,
  fetchOpenAIPlanJob,
  fetchOpenAIPlanModels,
  fetchOpenAIPlanStatus,
  testAndUseOpenAIPlan,
} from '../lib/api';
import type { OpenAIPlanCatalog, OpenAIPlanJob, OpenAIPlanStatus } from '../lib/api';

const JOB_STORAGE_KEY = 'easel_openai_plan_auth_job';
const JOB_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

export const DEVICE_CODE_COMMAND = 'openclaw --profile easel models auth login --provider openai --method device-code';
export const INTERACTIVE_LOGIN_COMMAND = 'openclaw --profile easel models auth login --provider openai --method siwc';
export const OAUTH_LOGIN_COMMAND = 'openclaw --profile easel models auth login --provider openai --method oauth';

export interface ChatGPTPlanSummary {
  usable: boolean;
  selectedModel: string;
}

export type PlanMessageKind = 'ok' | 'warn' | 'error';

interface StoredJob {
  jobId: string;
  method: 'siwc' | 'oauth';
}

const ERROR_COPY: Record<string, string> = {
  platform_fallback_present: '存在 Platform 计费来源，已安全阻止。',
  billing_ambiguity: '无法确认唯一订阅计费来源，请刷新并检查 OpenClaw。',
  profile_unusable: 'ChatGPT Plan 登录暂不可用，需要重新登录。',
  profile_activation_failed: '无法启用当前登录配置，请刷新后重试。',
  profile_activation_unconfirmed: '无法确认登录配置已启用，请刷新后重试。',
  model_test_unproven: '无法证明指定模型成功调用，未更改当前模型。',
  credential_proof_mismatch: '认证来源不匹配，已安全阻止。',
  catalog_empty: '当前账号未返回可用模型。',
  catalog_stale: '模型目录已过期，请刷新。',
  catalog_unavailable: '模型目录暂不可用，请稍后重试。',
  auth_failed: '登录失败，请重试。',
  auth_timeout: '登录等待超时，请重试。',
  auth_process_error: '无法启动 OpenClaw 登录，请检查本机环境。',
  gateway_unavailable: '本机 OpenClaw Gateway 暂不可用，请检查 Gateway 后重试。',
  gateway_client_unavailable: '当前 OpenClaw 无法提供安全登录连接，请检查版本后重试。',
  gateway_auth_unavailable: 'OpenClaw 当前未提供所选登录方式，请刷新后重试。',
  gateway_rpc_failed: 'OpenClaw 登录连接中断，请重试。',
  auth_browser_open_failed: '无法打开安全登录页面，请检查本机浏览器后重试。',
  unsupported_wizard_step: 'OpenClaw 返回了暂不支持的登录步骤，已安全取消。',
  oauth_choice_unavailable: 'OpenClaw 当前未提供 OAuth 登录方式，请刷新后重试。',
  auth_not_confirmed: 'OpenClaw 未确认订阅登录，请重试。',
  auth_cancelled: '登录已取消。',
  plan_auth_required: '请先使用 ChatGPT Plan 登录。',
  auth_status_unavailable: '暂时无法读取 OpenClaw 登录状态。',
};

function safeError(code: string, fallback: string): string {
  return ERROR_COPY[code] || fallback;
}

function loadStoredJob(): StoredJob | null {
  try {
    const raw = sessionStorage.getItem(JOB_STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<StoredJob>;
    if (!value.jobId || !JOB_ID_PATTERN.test(value.jobId)) return null;
    if (value.method !== 'siwc' && value.method !== 'oauth') return null;
    return { jobId: value.jobId, method: value.method };
  } catch {
    return null;
  }
}

function rememberJob(job: StoredJob | null): void {
  try {
    if (job) sessionStorage.setItem(JOB_STORAGE_KEY, JSON.stringify(job));
    else sessionStorage.removeItem(JOB_STORAGE_KEY);
  } catch {
    // Session persistence is optional; credential truth remains in OpenClaw.
  }
}

function restoredJobView(): OpenAIPlanJob | null {
  const stored = loadStoredJob();
  if (!stored) return null;
  return {
    jobId: stored.jobId,
    method: stored.method,
    state: 'running',
    phase: 'waiting',
    message: '',
    errorCode: '',
  };
}

export function isExclusivePlanUsable(status: OpenAIPlanStatus | null): boolean {
  return Boolean(
    status?.connected
    && status.usable
    && status.billingSource === 'chatgpt_plan'
    && status.activeProfileHandle,
  );
}

export function useChatGPTPlan(onSummaryChange?: (summary: ChatGPTPlanSummary) => void) {
  const mountedRef = useRef(true);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [status, setStatus] = useState<OpenAIPlanStatus | null>(null);
  const [catalog, setCatalog] = useState<OpenAIPlanCatalog | null>(null);
  const [job, setJob] = useState<OpenAIPlanJob | null>(restoredJobView);
  const [selectedModel, setSelectedModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [testing, setTesting] = useState(false);
  const [message, setMessage] = useState('');
  const [messageKind, setMessageKind] = useState<PlanMessageKind>('warn');
  const [copyNote, setCopyNote] = useState('');
  const [terminalCommand, setTerminalCommand] = useState('');

  const refreshStatus = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetchOpenAIPlanStatus();
      if (!mountedRef.current) return;
      setStatus(next);
      if (isExclusivePlanUsable(next)) {
        setCatalogLoading(true);
        try {
          const nextCatalog = await fetchOpenAIPlanModels();
          if (mountedRef.current) setCatalog(nextCatalog);
        } catch {
          if (mountedRef.current) {
            setCatalog({ status: 'unavailable', models: [], errorCode: 'catalog_unavailable' });
          }
        } finally {
          if (mountedRef.current) setCatalogLoading(false);
        }
      } else {
        setCatalog(null);
        setSelectedModel('');
      }
    } catch {
      if (!mountedRef.current) return;
      setStatus(null);
      setCatalog(null);
      setMessageKind('error');
      setMessage('暂时无法读取 ChatGPT Plan 状态，请刷新后重试。');
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void refreshStatus();
    return () => {
      mountedRef.current = false;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, [refreshStatus]);

  useEffect(() => {
    onSummaryChange?.({
      usable: isExclusivePlanUsable(status),
      selectedModel: status && isExclusivePlanUsable(status) ? status.selectedModel : '',
    });
  }, [onSummaryChange, status]);

  useEffect(() => {
    if (catalog?.status !== 'available') {
      setSelectedModel('');
      return;
    }
    const available = catalog.models.filter(
      (model) => model.provider === 'openai'
        && model.ref.startsWith('openai/')
        && model.availability === 'available',
    );
    setSelectedModel((current) => {
      if (available.some((model) => model.ref === current)) return current;
      if (status?.selectedModel && available.some((model) => model.ref === status.selectedModel)) {
        return status.selectedModel;
      }
      return available[0]?.ref || '';
    });
  }, [catalog, status?.selectedModel]);

  const finishJob = useCallback(async (next: OpenAIPlanJob) => {
    rememberJob(null);
    setJob(null);
    if (next.state === 'success') {
      setMessageKind('ok');
      setMessage('登录已完成，正在刷新 ChatGPT Plan 状态。');
      await refreshStatus();
    } else if (next.state === 'cancelled') {
      setMessageKind('warn');
      setMessage('登录已取消。');
    } else if (next.state === 'interaction_required') {
      setMessageKind('warn');
      const deviceCode = next.method === 'device-code';
      const oauth = next.method === 'oauth';
      setMessage(deviceCode
        ? '请在本机终端运行 OpenClaw device-code 登录命令。'
        : 'Gateway 暂不可用，请在本机终端运行 OpenClaw 登录命令。');
      setTerminalCommand(deviceCode ? DEVICE_CODE_COMMAND : oauth ? OAUTH_LOGIN_COMMAND : INTERACTIVE_LOGIN_COMMAND);
    } else {
      setMessageKind('error');
      setMessage(safeError(next.errorCode, '登录失败，请重试。'));
    }
  }, [refreshStatus]);

  useEffect(() => {
    if (!job?.jobId || job.state !== 'running') return undefined;
    let disposed = false;

    const poll = async () => {
      try {
        const next = await fetchOpenAIPlanJob(job.jobId);
        if (disposed || !mountedRef.current) return;
        if (next.state === 'running') {
          setJob(next);
          pollTimerRef.current = setTimeout(() => void poll(), 1200);
        } else {
          await finishJob(next);
        }
      } catch {
        if (disposed || !mountedRef.current) return;
        rememberJob(null);
        setJob(null);
        setMessageKind('error');
        setMessage('登录任务已失效，请重新开始。');
      }
    };

    void poll();
    return () => {
      disposed = true;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, [finishJob, job?.jobId, job?.state]);

  const startConnect = useCallback(async (method: 'siwc' | 'oauth') => {
    setStarting(true);
    setMessage('');
    setTerminalCommand('');
    try {
      const next = await connectOpenAIPlan(method);
      if (!mountedRef.current) return;
      if (next.state === 'running') {
        rememberJob({ jobId: next.jobId, method });
        setJob(next);
      } else {
        await finishJob(next);
      }
    } catch (error) {
      if (!mountedRef.current) return;
      const conflict = error instanceof Error && error.message.includes('已有 OpenAI 登录任务正在运行');
      setMessageKind(conflict ? 'warn' : 'error');
      setMessage(conflict
        ? '已有登录任务正在运行；请等待完成，或刷新页面恢复本窗口的任务。'
        : '无法启动 ChatGPT 登录，请检查 OpenClaw 后重试。');
    } finally {
      if (mountedRef.current) setStarting(false);
    }
  }, [finishJob]);

  const cancelJob = useCallback(async () => {
    if (!job?.jobId) return;
    try {
      await cancelOpenAIPlanJob(job.jobId);
      rememberJob(null);
      setJob(null);
      setMessageKind('warn');
      setMessage('登录已取消。');
    } catch {
      setMessageKind('error');
      setMessage('取消登录失败；任务可能已经结束，请刷新状态。');
    }
  }, [job?.jobId]);

  const refreshCatalog = useCallback(async () => {
    if (!status || !isExclusivePlanUsable(status)) return;
    setCatalogLoading(true);
    try {
      const next = await fetchOpenAIPlanModels();
      if (mountedRef.current) setCatalog(next);
    } catch {
      if (mountedRef.current) setCatalog({ status: 'unavailable', models: [], errorCode: 'catalog_unavailable' });
    } finally {
      if (mountedRef.current) setCatalogLoading(false);
    }
  }, [status]);

  const testAndUse = useCallback(async () => {
    if (!status || !isExclusivePlanUsable(status) || !selectedModel) return;
    setTesting(true);
    setMessage('');
    try {
      const result = await testAndUseOpenAIPlan(status.activeProfileHandle, selectedModel);
      if (!mountedRef.current) return;
      if (result.ok) {
        setMessageKind('ok');
        setMessage(`已验证并启用 ${result.selectedModel}`);
        await refreshStatus();
      } else {
        setMessageKind('error');
        setMessage(safeError(result.errorCode, '验证失败，未更改当前模型。'));
      }
    } catch {
      if (!mountedRef.current) return;
      setMessageKind('error');
      setMessage('验证失败，Profile 或模型可能已失效，请刷新后重试。');
    } finally {
      if (mountedRef.current) setTesting(false);
    }
  }, [refreshStatus, selectedModel, status]);

  const openAIModels = useMemo(
    () => (catalog?.models || []).filter(
      (model) => model.provider === 'openai' && model.ref.startsWith('openai/'),
    ),
    [catalog],
  );
  const chosenAvailable = openAIModels.some(
    (model) => model.ref === selectedModel && model.availability === 'available',
  );
  const busy = starting || job?.state === 'running';
  const canTest = isExclusivePlanUsable(status) && chosenAvailable && !busy && !testing;

  const copyDeviceCommand = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(DEVICE_CODE_COMMAND);
      setCopyNote('已复制');
    } catch {
      setCopyNote('请手动复制');
    }
  }, []);

  const copyInteractiveCommand = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(terminalCommand || INTERACTIVE_LOGIN_COMMAND);
      setCopyNote('已复制');
    } catch {
      setCopyNote('请手动复制');
    }
  }, [terminalCommand]);

  return {
    status,
    catalog,
    job,
    selectedModel,
    setSelectedModel,
    loading,
    catalogLoading,
    testing,
    message,
    messageKind,
    copyNote,
    terminalCommand,
    openAIModels,
    busy,
    canTest,
    startConnect,
    cancelJob,
    refreshCatalog,
    testAndUse,
    copyDeviceCommand,
    copyInteractiveCommand,
  };
}
