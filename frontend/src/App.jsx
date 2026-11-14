import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Landing from './Landing.jsx';
import MyContext from './MyContext.jsx';
import { Activity, AlertCircle, Bot, Check, CheckCircle2, ChevronRight, Circle, ClipboardCheck, Clock3, Code, Copy, CreditCard, Database, Download, ExternalLink, FilePenLine, FileText, Globe, Layers, Lightbulb, LoaderCircle, Paperclip, Pause, Play, RefreshCw, ScanSearch, Search, Send, ShieldCheck, Square, Table, Target, Trash2, X, Zap } from 'lucide-react';

const TERMINAL = ['completed', 'failed', 'cancelled'];
const ACTIVE = ['pending', 'running', 'waiting_approval', 'paused'];

// Set VITE_API_BASE_URL when the frontend is deployed separately from FastAPI.
// Leave it empty when the backend is served from the same origin.

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '');
const WS_BASE_URL = (import.meta.env.VITE_WS_BASE_URL || '').replace(/\/+$/, '');

const api = (path) => `${API_BASE_URL}${path}`;
const wsUrl = (path) => {
  if (WS_BASE_URL) return `${WS_BASE_URL}${path}`;
  if (API_BASE_URL) {
    const backendUrl = new URL(API_BASE_URL, window.location.origin);
    const protocol = backendUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${backendUrl.host}${path}`;
  }
  return (window.location.protocol === 'https:' ? 'wss://' : 'ws://') +
    window.location.host +
    path;
};

function isSafeWebUrl(url) {
  if (!url || typeof url !== 'string') return false;
  const trimmed = url.trim();
  // Strip control characters (including tabs and newlines that some browsers ignore in protocol parsing)
  const clean = trimmed.replace(/[\x00-\x1F\x7F-\x9F]/g, '');
  // Explicitly disallow dangerous executable schemes
  if (/^(javascript|data|vbscript|blob|file):/i.test(clean)) return false;
  // Strictly allow only http://, https://, or single-slash relative / URLs (disallow protocol-relative // or /\)
  if (/^https?:\/\//i.test(clean)) return true;
  if (/^\/(?!\/|\\)/.test(clean)) return true;
  return false;
}

let currentSessionId = null;
let currentCsrfToken = null;
let sessionPromise = null;
let sessionBackoffUntil = 0;

async function ensureSession() {
  if (currentSessionId && currentCsrfToken) {
    return { sessionId: currentSessionId, csrfToken: currentCsrfToken };
  }
  if (Date.now() < sessionBackoffUntil) {
    return { sessionId: currentSessionId, csrfToken: currentCsrfToken };
  }
  if (!sessionPromise) {
    sessionPromise = (async () => {
      try {
        // Direct fetch without authFetch to avoid recursive promise deadlocks
        const res = await fetch(api('/api/auth/session'), {
          method: 'POST',
          credentials: 'include',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
          },
        });
        if (res.ok) {
          const data = await res.json();
          currentSessionId = data.session_id;
          currentCsrfToken = data.csrf_token;
          return { sessionId: currentSessionId, csrfToken: currentCsrfToken };
        } else if (res.status === 429) {
          console.warn('Session rate limit encountered, backing off for 5s.');
          sessionBackoffUntil = Date.now() + 5000;
        } else if (res.status === 401 || res.status === 403) {
          console.warn('Session initialization rejected (' + res.status + ').');
          sessionBackoffUntil = Date.now() + 3000;
        }
      } catch (e) {
        console.warn('Session initialization network error:', e);
        sessionBackoffUntil = Date.now() + 2000;
      } finally {
        sessionPromise = null;
      }
      return { sessionId: currentSessionId, csrfToken: currentCsrfToken };
    })();
  }
  return sessionPromise;
}

async function authFetch(url, options = {}, isRetry = false) {
  const session = await ensureSession();
  const headers = { ...(options.headers || {}) };
  const method = (options.method || 'GET').toUpperCase();
  if (session?.csrfToken && ['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
    headers['X-CSRF-Token'] = session.csrfToken;
  }
  if (session?.sessionId && !headers['Authorization']) {
    headers['Authorization'] = `Bearer ${session.sessionId}`;
  }
  const opt = {
    ...options,
    credentials: 'include',
    headers,
  };
  const res = await fetch(url, opt);
  // Only retry once on 401, never retry on auth/session, and only if we refreshed a valid session
  if (res.status === 401 && !isRetry && !url.includes('/api/auth/session')) {
    currentSessionId = null;
    currentCsrfToken = null;
    const refreshed = await ensureSession();
    if (refreshed?.sessionId) {
      return authFetch(url, options, true);
    }
  }
  return res;
}

const isActive = (status) => ACTIVE.includes(status);
const isTerminal = (status) => TERMINAL.includes(status);

const formatUserFacingError = (error) => {
  if (!error) return '';
  const errStr = typeof error === 'string' ? error : (error.message || JSON.stringify(error));
  const lower = errStr.toLowerCase();

  if (lower.includes('extraction') && (lower.includes('threshold') || lower.includes('sensitive') || lower.includes('refused') || lower.includes('protected'))) {
    return 'The requested content contains protected information. For security reasons, the data cannot be extracted.';
  }
  if (lower.includes('upload') && (lower.includes('restrict') || lower.includes('sensitive') || lower.includes('allowlist') || lower.includes('prohibit'))) {
    return 'The selected file cannot be uploaded due to security restrictions.';
  }
  if (lower.includes('navigation') && (lower.includes('blocked') || lower.includes('restrict') || lower.includes('prohibit') || lower.includes('loopback'))) {
    return 'Navigation to this address is restricted by security policy.';
  }
  if (lower.includes('screenshot') && (lower.includes('protect') || lower.includes('masking') || lower.includes('origin'))) {
    return 'Screen capture unavailable for protected content.';
  }
  if (lower.includes('password or otp input') || lower.includes('enter your credentials')) {
    return 'Authentication or security verification required. Please complete this step directly in the browser to continue.';
  }
  if (lower.includes('tier 1') || lower.includes('tier 2') || lower.includes('tier 3') || lower.includes('tier1') || lower.includes('raw tier') || lower.includes('harvest prohibited')) {
    return 'This task involves accessing or retrieving protected information. For security reasons, the action cannot be completed.';
  }
  return errStr.replace(/\[REDACTED(?:_[A-Z0-9_]+)?\]/gi, '[protected]').replace(/\{\{[A-Z0-9_]+\}\}/g, '[protected]');
};


const statusLabel = (task) => {
  if (task.status === 'waiting_approval') return 'Needs approval';
  if (task.status === 'paused') return 'Paused';
  if (task.status === 'pending') return 'Queued';
  if (task.status === 'running')
    return task.plan_status === 'planning' ? 'Planning' : 'Running';
  if (task.status === 'completed') return 'Completed';
  if (task.status === 'failed') return 'Failed';
  if (task.status === 'cancelled') return 'Cancelled';
  return task.status || 'Unknown';
};

const formatDuration = (value) => {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return Math.round(ms) + ' ms';
  const seconds = ms / 1000;
  if (seconds < 60) return seconds.toFixed(2) + ' sec';
  const mins = Math.floor(seconds / 60);
  const rest = seconds - mins * 60;
  return rest < 0.05 ? `${mins} min` : `${mins} min ${rest.toFixed(1)} sec`;
};

const milestoneIcon = (text) => {
  const value = String(text || '').toLowerCase();
  if (/navigat|open|portal|page/.test(value)) return Globe;
  if (/search|query|filter|find/.test(value)) return Search;
  if (/inspect|discover|structure/.test(value)) return ScanSearch;
  if (/fill|enter|select|form/.test(value)) return FilePenLine;
  if (/upload|resume|document|file/.test(value)) return Paperclip;
  if (/approval|confirm|review|verify/.test(value)) return ShieldCheck;
  if (/submit|finalize|checkout|apply/.test(value)) return Send;
  return Circle;
};

const extractPlanSteps = (task) => {
  if (!task) return [];
  const candidates = [
    task.initial_plan,
    task.plan,
    task.execution_plan,
    task.plan_metadata?.plan,
    task.plan_metadata?.initial_plan,
    task.plan_metadata?.cache_details?.plan,
    task.plan_metadata?.steps,
    task.plan_metadata?.milestones,
    task.context?.plan,
  ];

  for (const candidate of candidates) {
    if (!candidate) continue;

    if (Array.isArray(candidate) && candidate.length > 0) {
      const extracted = candidate
        .map((item) => {
          if (!item) return '';
          if (typeof item === 'string') return item.trim();
          if (typeof item === 'object') {
            return (
              item.description ||
              item.title ||
              item.milestone ||
              item.step ||
              item.text ||
              item.action ||
              item.name ||
              JSON.stringify(item)
            ).trim();
          }
          return String(item).trim();
        })
        .filter(Boolean);
      if (extracted.length > 0) return extracted;
    }

    if (typeof candidate === 'string' && candidate.trim()) {
      const trimmed = candidate.trim();
      if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
        try {
          const parsed = JSON.parse(trimmed);
          if (Array.isArray(parsed) && parsed.length > 0) {
            return extractPlanSteps({ initial_plan: parsed });
          }
        } catch {
          /* ignore */
        }
      }
      if (trimmed.includes('\n')) {
        const lines = trimmed
          .split('\n')
          .map((line) => line.replace(/^\s*(\d+[\.\)]|\-|\*)\s*/, '').trim())
          .filter(Boolean);
        if (lines.length > 0) return lines;
      }
      return [trimmed];
    }
  }

  return [];
};

const normalizeTask = (task) => {
  if (!task) return null;
  const initial_plan = extractPlanSteps(task);
  const steps = Array.isArray(task.steps) ? task.steps : [];
  const directResults = Array.isArray(task.extracted_data)
    ? task.extracted_data
    : task.extracted_data
      ? [task.extracted_data]
      : [];
  // Recover extraction payloads attached to action steps when a task-level
  // result list is absent from a WebSocket/history snapshot.
  // Only treat step payloads as extracted results when the step actually
  // performed an EXTRACT action. Fill/submit/finish actions can carry useful
  // metadata (for example, fields filled or a completion status), but that is
  // not page extraction and should not create the Extracted results panel.
  const stepResults = steps
    .filter((step) => {
      const action = typeof step?.action === 'string'
        ? step.action
        : step?.action?.action;
      return String(action || '').toLowerCase() === 'extract' &&
        step?.extracted_data !== null &&
        step?.extracted_data !== undefined;
    })
    .map((step) => step.extracted_data)
    .filter((value) => value !== '' && !(Array.isArray(value) && value.length === 0));
  const hasExtractionStep = steps.some((step) => {
    const action = typeof step?.action === 'string'
      ? step.action
      : step?.action?.action;
    return String(action || '').toLowerCase() === 'extract';
  });
  const extracted_data = hasExtractionStep
    ? (directResults.length ? directResults : stepResults)
    : [];
  return {
    ...task,
    steps,
    initial_plan,
    plan: initial_plan,
    reports: task.reports || {},
    extracted_data,
    context: task.context || {},
  };
};

const mergeTask = (prevTask, incomingTask) => {
  if (!prevTask) return normalizeTask(incomingTask);
  if (!incomingTask) return prevTask;

  const normalized = normalizeTask(incomingTask);
  if (!normalized) return prevTask;

  // Extract plan steps from both: NEVER discard an existing plan for an empty one!
  const prevPlan = extractPlanSteps(prevTask);
  const incomingPlan = extractPlanSteps(normalized);
  const mergedPlan = incomingPlan.length > 0 ? incomingPlan : prevPlan;

  // Plan metadata & cache details: preserve previous metadata if incoming is empty/missing
  const prevMeta = (prevTask.plan_metadata && typeof prevTask.plan_metadata === 'object') ? prevTask.plan_metadata : {};
  const incomingMeta = (normalized.plan_metadata && typeof normalized.plan_metadata === 'object') ? normalized.plan_metadata : {};
  const plan_metadata =
    Object.keys(incomingMeta).length > 0
      ? {
        ...prevMeta,
        ...incomingMeta,
        cache_details: incomingMeta.cache_details || prevMeta.cache_details,
      }
      : (Object.keys(prevMeta).length > 0 ? prevMeta : normalized.plan_metadata || prevTask.plan_metadata);

  // Context merging: preserve nested context fields so live data never disappears
  const prevContext = (prevTask.context && typeof prevTask.context === 'object') ? prevTask.context : {};
  const incomingContext = (normalized.context && typeof normalized.context === 'object') ? normalized.context : {};

  const visited_urls =
    incomingContext.visited_urls && incomingContext.visited_urls.length > 0
      ? incomingContext.visited_urls
      : (prevContext.visited_urls || []);

  const action_history =
    incomingContext.action_history && incomingContext.action_history.length > 0
      ? incomingContext.action_history
      : (prevContext.action_history || []);

  const filled_fields =
    incomingContext.filled_fields && Object.keys(incomingContext.filled_fields).length > 0
      ? { ...(prevContext.filled_fields || {}), ...incomingContext.filled_fields }
      : (prevContext.filled_fields || {});

  const recent_errors =
    incomingContext.recent_errors && incomingContext.recent_errors.length > 0
      ? incomingContext.recent_errors
      : (prevContext.recent_errors || []);

  const extracted_snippets =
    incomingContext.extracted_snippets && incomingContext.extracted_snippets.length > 0
      ? incomingContext.extracted_snippets
      : (prevContext.extracted_snippets || []);

  const mergedContext = {
    ...prevContext,
    ...incomingContext,
    visited_urls,
    action_history,
    filled_fields,
    recent_errors,
    extracted_snippets,
  };

  // Steps: keep the longer or incoming list
  const prevSteps = Array.isArray(prevTask.steps) ? prevTask.steps : [];
  const incomingSteps = Array.isArray(normalized.steps) ? normalized.steps : [];
  const steps = incomingSteps.length >= prevSteps.length ? incomingSteps : prevSteps;

  // Extracted data: if the incoming task has a known action history with no
  // EXTRACT action, clear any stale extraction payload retained from an earlier
  // snapshot. Preserve prior data only for partial snapshots that contain no
  // usable step history at all.
  const prevExtracted = Array.isArray(prevTask.extracted_data) ? prevTask.extracted_data : [];
  const incomingExtracted = Array.isArray(normalized.extracted_data) ? normalized.extracted_data : [];
  const incomingHasExtractionStep = incomingSteps.some((step) => {
    const action = typeof step?.action === 'string' ? step.action : step?.action?.action;
    return String(action || '').toLowerCase() === 'extract';
  });
  const hasIncomingActionHistory = incomingSteps.length > 0;
  const extracted_data = incomingHasExtractionStep
    ? (incomingExtracted.length > 0 ? incomingExtracted : prevExtracted)
    : hasIncomingActionHistory
      ? []
      : (incomingExtracted.length > 0 ? incomingExtracted : prevExtracted);

  // Current plan step: advance forward, do not regress to 0 on partial updates
  const current_plan_step = Math.max(
    Number(prevTask.current_plan_step || 0),
    Number(normalized.current_plan_step || 0)
  );

  return {
    ...prevTask,
    ...normalized,
    initial_plan: mergedPlan,
    plan: mergedPlan,
    plan_metadata,
    context: mergedContext,
    steps,
    extracted_data,
    current_plan_step,
  };
};

function IconButton({ title, onClick, children, danger = false, disabled = false }) {
  return (
    <button
      type="button"
      className={'icon-button' + (danger ? ' danger' : '')}
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}

function EmptyState({ icon: Icon = Activity, title, description }) {
  return (
    <div className="empty-state">
      <Icon size={28} />
      <strong>{title}</strong>
      <span>{description}</span>
    </div>
  );
}

function StatusPill({ task }) {
  return (
    <span className={'status-pill status-' + (task.status || 'pending')}>
      <span className="status-dot" />
      {statusLabel(task)}
    </span>
  );
}

function TaskTab({ task, selected, onSelect, onPause, onResume, onCancel, onDismiss }) {
  const count = task.steps?.length || task.current_step || 0;

  return (
    <div className={'task-tab' + (selected ? ' selected' : '')}>
      <span className={'task-tab-dot status-dot-' + (task.status || 'pending')} />
      <button
        type="button"
        className="task-tab-select"
        onClick={onSelect}
        title={task.goal || task.task_id}
        aria-pressed={selected}
      >
        <span className="task-tab-copy">
          <strong>{task.goal || task.task_id}</strong>
          <small>Step {count}/{task.max_steps || 15} · {statusLabel(task)}</small>
        </span>
      </button>

      {isActive(task.status) ? (
        <>
          {task.status === 'paused' ? (
            <button type="button" className="tab-action resume" title="Resume task" aria-label="Resume task" onClick={onResume}>
              <Play size={13} strokeWidth={3} />
            </button>
          ) : task.status === 'running' ? (
            <button type="button" className="tab-action pause" title="Pause task" aria-label="Pause task" onClick={onPause}>
              <Pause size={13} strokeWidth={3} />
            </button>
          ) : null}
          <button type="button" className="tab-action stop" title="Stop task" aria-label="Stop task" onClick={onCancel}>
            <Square size={11} />
          </button>
        </>
      ) : (
        <button type="button" className="tab-action" title="Dismiss task" aria-label="Dismiss task" onClick={onDismiss}>
          <X size={12} />
        </button>
      )}
    </div>
  );
}

function ActionCard({ step }) {
  const action =
    typeof step.action === 'string'
      ? step.action
      : step.action?.action || 'action';
  const isApprovalAction = String(action).toLowerCase().includes('approval');
  const cacheHit = String(step.reasoning || '')
    .toLowerCase()
    .includes('redis cache hit');

  return (
    <article
      className={
        'action-card' +
        (step.error && !isApprovalAction ? ' action-failed' : '') +
        (isApprovalAction ? ' action-approval' : '') +
        (cacheHit ? ' cache-hit' : '')
      }
    >
      <div className="action-head">
        <div className="action-name">
          <span className="step-number">#{step.step_number ?? '?'}</span>
          <span className={'action-tag tag-' + String(action).toLowerCase()}>
            {String(action).replaceAll('_', ' ')}
          </span>
          {cacheHit && (
            <span className="cache-tag">
              <Zap size={12} /> Redis hit
            </span>
          )}
        </div>

        <div className="action-meta">
          {step.duration_ms != null && (
            <span>
              <Clock3 size={13} />
              {formatDuration(step.duration_ms)}
            </span>
          )}
          {step.error && !isApprovalAction ? (
            <span className="failed-text">
              <AlertCircle size={14} /> Failed
            </span>
          ) : isApprovalAction ? (
            <span className="warn-text">
              <ShieldCheck size={14} /> Approved
            </span>
          ) : (
            <span className="success-text">
              <CheckCircle2 size={14} /> Executed
            </span>
          )}
        </div>
      </div>

      <p className="action-description">{step.description || 'Action completed.'}</p>

      {step.reasoning && (
        <p className="action-reasoning">
          <Lightbulb size={14} />
          {step.reasoning.split(' | Error Recovery Attempts:')[0]}
        </p>
      )}

      {step.error &&
        step.description !== step.error &&
        !String(step.description || '').includes(String(step.error || '').slice(0, 40)) && (
          <p className="error-text">{formatUserFacingError(step.error)}</p>
      )}

      {step.url && (
        isSafeWebUrl(step.url) ? (
          <a className="inline-link" href={step.url} target="_blank" rel="noopener noreferrer">
            <ExternalLink size={13} />
            {step.url}
          </a>
        ) : (
          <span className="inline-link" title="Untrusted or non-navigable URL scheme">
            <ExternalLink size={13} />
            {step.url}
          </span>
        )
      )}

      {String(typeof step.action === 'string' ? step.action : step.action?.action || '').toLowerCase() === 'extract' && step.extracted_data != null && (
        <details className="data-details">
          <summary>Extracted data</summary>
          <ExtractedResultsViewer data={step.extracted_data} compact />
        </details>
      )}

      {step.screenshot_url && (
        <a
          className="inline-link"
          href={api(step.screenshot_url)}
          target="_blank"
          rel="noreferrer"
        >
          View screenshot <ExternalLink size={13} />
        </a>
      )}
    </article>
  );
}

function ReportDownloadButton({ task }) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState('');

  if (!task || !task.task_id || !isTerminal(task.status)) return null;

  const downloadReport = async () => {
    if (downloading) return;
    setDownloading(true);
    setDownloadError('');

    try {
      const reportPath = task.reports?.pdf || `/api/reports/${encodeURIComponent(task.task_id)}/download`;
      if (!isSafeWebUrl(reportPath)) {
        throw new Error('The report download address is invalid.');
      }

      // Report paths returned by FastAPI are usually relative (/api/...).
      // Resolve them against the configured backend origin, not the frontend host.
      const isAbsoluteReportUrl = reportPath.startsWith('https://') || reportPath.startsWith('http://');
      const reportUrl = isAbsoluteReportUrl
        ? reportPath
        : api(reportPath.startsWith('/') ? reportPath : `/${reportPath}`);

      const response = await authFetch(reportUrl, {
        method: 'GET',
        headers: { Accept: 'application/pdf' },
      });

      if (!response.ok) {
        let detail = '';
        try {
          const payload = await response.json();
          detail = payload.detail || payload.message || '';
        } catch {
          // The server may return a plain-text or HTML error response.
        }
        throw new Error(detail || `Report download failed (HTTP ${response.status}).`);
      }

      const contentType = response.headers.get('content-type') || '';
      if (!contentType.toLowerCase().includes('application/pdf')) {
        throw new Error('The server did not return a PDF report.');
      }

      const blob = await response.blob();
      if (!blob.size) throw new Error('The generated report is empty.');

      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = `report-${String(task.task_id).replace(/[^a-zA-Z0-9_-]/g, '_')}.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (error) {
      console.error('Report download failed:', error);
      setDownloadError(error?.message || 'Unable to download the report.');
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="report-download-wrap">
      <button
        type="button"
        className="report-download-btn"
        onClick={downloadReport}
        disabled={downloading}
        title={downloadError || 'Download complete PDF task report'}
        aria-label="Download complete PDF task report"
      >
        <FileText size={15} />
        <span>{downloading ? 'Preparing PDF…' : 'Download Report'}</span>
        <Download size={13} />
      </button>
      {downloadError && (
        <span className="report-download-error" role="alert">
          {downloadError}
        </span>
      )}
    </div>
  );
}

function downloadCSV(rows, filename = 'extracted-data.csv') {
  if (!rows || !rows.length) return;
  const keys = Array.from(new Set(rows.flatMap((r) => Object.keys(r || {}))));
  const csvContent = [
    keys.map((k) => `"${k.replaceAll('_', ' ')}"`).join(','),
    ...rows.map((row) =>
      keys
        .map((k) => {
          let val = row[k];
          if (val === null || val === undefined) val = '';
          else if (typeof val === 'object') val = JSON.stringify(val);
          val = String(val);
          // CSV Formula Injection mitigation: prefix formula-initiating characters with a single quote
          const trimmed = val.trim();
          if (/^[=+\-@\t\r]/.test(trimmed)) {
            val = "'" + val;
          }
          val = val.replace(/"/g, '""');
          return `"${val}"`;
        })
        .join(',')
    ),
  ].join('\n');

  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.setAttribute('href', url);
  link.setAttribute('download', filename);
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  // Allow the browser to begin consuming the Blob before releasing it.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function parseExtractedData(data) {
  if (!data) return { type: 'empty', raw: null };

  let raw = data;
  if (Array.isArray(raw) && raw.length === 1 && typeof raw[0] === 'object' && raw[0] !== null) {
    raw = raw[0];
  }

  // 1. Primitive string, number, boolean
  if (typeof raw !== 'object' || raw === null) {
    return {
      type: 'text',
      text: String(raw),
      raw: data,
    };
  }

  // 2. Arrays: distinguish actual row collections from multiple extraction payloads.
  if (Array.isArray(raw)) {
    if (raw.length === 0) return { type: 'empty', raw: data };
    const allObjects = raw.every((item) => item && typeof item === 'object' && !Array.isArray(item));
    if (allObjects) {
      const payloadKeys = new Set([
        'page_title', 'title', 'url', 'canonical_url', 'headings', 'paragraphs',
        'links', 'metadata', 'page_metadata', 'text', 'content', 'images',
        'products', 'search_results', 'extracted_at',
      ]);
      const containsPayload = raw.some((item) =>
        Object.keys(item).some((key) => payloadKeys.has(key.toLowerCase()))
      );
      // EXTRACT actions can produce a list of separate page snapshots. Render
      // each snapshot using its own natural shape instead of turning snapshots
      // into rows in one giant table.
      if (containsPayload) {
        return { type: 'collection', items: raw, raw: data };
      }
      return {
        type: 'sections',
        summary: {},
        sections: [{ title: 'Results', rows: raw }],
        raw: data,
      };
    }
    // Arrays of primitives (e.g. headings, tags, URLs) should remain lists.
    return { type: 'list', items: raw, raw: data };
  }

  // 3. Objects are displayed as labelled fields. Nested arrays/objects are
  // rendered by CellValue according to their own shape (list, nested table, etc.).
  // Do not coerce a mixed object into a top-level table just because it contains
  // arrays or nested metadata.
  return {
    type: 'key_value',
    data: raw,
    raw: data,
  };
}

function CellValue({ col, val }) {
  if (val === null || val === undefined || val === '') {
    return <span className="null-val">—</span>;
  }

  // Boolean
  if (typeof val === 'boolean') {
    return (
      <span className={'status-tag ' + (val ? 'status-ok' : 'status-err')}>
        {val ? 'Yes' : 'No'}
      </span>
    );
  }

  // Nested Array
  if (Array.isArray(val)) {
    if (val.length === 0) return <span className="null-val">—</span>;
    if (typeof val[0] === 'object' && val[0] !== null) {
      const subKeys = Array.from(new Set(val.flatMap((item) => Object.keys(item || {}))));
      return (
        <div className="sub-table-wrap">
          <table className="results-table sub-table">
            <thead>
              <tr>
                {subKeys.map((k) => (
                  <th key={k}>{k.replaceAll('_', ' ')}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {val.map((subRow, sIdx) => (
                <tr key={sIdx}>
                  {subKeys.map((k) => (
                    <td key={k}>
                      <CellValue col={k} val={subRow[k]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    return (
      <div className="tag-list">
        {val.map((item, idx) => (
          <span className="pill-tag" key={idx}>
            {String(item)}
          </span>
        ))}
      </div>
    );
  }

  // Nested Object
  if (typeof val === 'object') {
    return (
      <div className="kv-grid">
        {Object.entries(val).map(([k, v]) => (
          <span className="kv-badge" key={k}>
            <small>{k.replaceAll('_', ' ')}:</small> <CellValue col={k} val={v} />
          </span>
        ))}
      </div>
    );
  }

  const str = String(val).trim();
  const colLower = String(col || '').toLowerCase();

  // Image URL
  if (/^https?:\/\/.*\.(png|jpg|jpeg|webp|gif|svg)(\?.*)?$/i.test(str)) {
    return (
      <a href={str} target="_blank" rel="noreferrer" className="cell-img-link" title="Open full image">
        <img src={str} alt={col} className="cell-thumb" />
      </a>
    );
  }

  // General URL
  if (/^https?:\/\//i.test(str)) {
    const isUrlShort = str.length < 42;
    return (
      <a href={str} target="_blank" rel="noreferrer" className="table-link">
        {isUrlShort ? str : str.replace(/^https?:\/\/(www\.)?/, '').slice(0, 32) + '…'} <ExternalLink size={11} />
      </a>
    );
  }

  // Email
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(str)) {
    return (
      <a href={`mailto:${str}`} className="table-link">
        {str}
      </a>
    );
  }

  // GitHub repo pattern (e.g. "vercel-labs/agent-browser")
  if (/^[a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+$/.test(str) && (colLower.includes('repo') || colLower.includes('name') || !colLower.includes('path'))) {
    return (
      <a
        href={`https://github.com/${str}`}
        target="_blank"
        rel="noreferrer"
        className="table-link repo-link"
      >
        {str} <ExternalLink size={11} />
      </a>
    );
  }

  // Currency
  if (/^[$€£₹]\s?[\d,]+(\.\d{1,2})?$/.test(str) || /^\d+(\.\d{1,2})?\s?(USD|EUR|GBP|INR)$/i.test(str)) {
    return <span className="currency-badge">{str}</span>;
  }

  // Star ratings / reviews / counts
  if (colLower.includes('star') || colLower.includes('rating') || colLower.includes('score')) {
    return <span className="star-badge">⭐ {str.replace(/^⭐\s?/, '')}</span>;
  }

  // Status badges
  if (colLower.includes('status') || ['success', 'active', 'completed', 'approved', 'failed', 'pending', 'cancelled', 'in stock', 'out of stock'].includes(str.toLowerCase())) {
    const s = str.toLowerCase();
    const isOk = ['success', 'active', 'completed', 'approved', 'in stock', 'true', 'yes'].includes(s);
    const isErr = ['failed', 'error', 'rejected', 'out of stock', 'cancelled', 'false', 'no'].includes(s);
    return (
      <span className={'status-tag ' + (isOk ? 'status-ok' : isErr ? 'status-err' : 'status-warn')}>
        {str}
      </span>
    );
  }

  // Language / Category tags
  if (colLower.includes('lang') || colLower.includes('category') || colLower.includes('tag')) {
    return <span className="lang-tag">{str}</span>;
  }

  return <span className="cell-value">{str}</span>;
}

function stripAgentInternalFields(value) {
  if (Array.isArray(value)) return value.map(stripAgentInternalFields);
  if (!value || typeof value !== 'object') return value;

  return Object.fromEntries(
    Object.entries(value)
      .filter(([key, entry]) => {
        const normalizedKey = key.toLowerCase().replaceAll('-', '_');
        if (/subagents?|specialists?|agent_team|recovery_agent|followup_delegation|independent_verification|delegation/.test(normalizedKey)) return false;
        if (normalizedKey === 'source' && String(entry).toLowerCase() === 'runtime_subagent_batch') return false;
        return true;
      })
      .map(([key, entry]) => [key, stripAgentInternalFields(entry)])
  );
}

function ExtractedResultsViewer({ data, compact = false }) {
  const [viewMode, setViewMode] = useState('table');
  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState(0);
  const [filterText, setFilterText] = useState('');

  const displayData = useMemo(() => stripAgentInternalFields(data), [data]);
  const parsed = useMemo(() => parseExtractedData(displayData), [displayData]);
  const { type, summary = {}, sections = [], data: kvData, items = [], text = '', raw } = parsed;

  const currentSection = sections[activeTab] || sections[0] || null;
  const rawRows = currentSection?.rows || [];

  const filteredRows = useMemo(() => {
    if (!filterText.trim()) return rawRows;
    const q = filterText.toLowerCase();
    return rawRows.filter((row) =>
      Object.values(row || {}).some((v) => String(v ?? '').toLowerCase().includes(q))
    );
  }, [rawRows, filterText]);

  const columns = useMemo(() => {
    if (!rawRows.length) return [];
    const keys = new Set();
    rawRows.forEach((r) => Object.keys(r || {}).forEach((k) => keys.add(k)));
    return Array.from(keys);
  }, [rawRows]);

  const copyToClipboard = () => {
    try {
      navigator.clipboard.writeText(JSON.stringify(raw ?? displayData, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard fallback */
    }
  };

  if (type === 'empty') return null;

  if (type === 'collection') {
    return (
      <div className={'extracted-results-container extracted-collection' + (compact ? ' is-compact' : '')}>
        {items.map((item, index) => (
          <section className="extracted-item" key={index}>
            <h3 className="extracted-item-title">
              {item.page_title || item.title || item.url || `Extracted item ${index + 1}`}
            </h3>
            <ExtractedResultsViewer data={item} compact />
          </section>
        ))}
      </div>
    );
  }

  return (
    <div className={'extracted-results-container' + (compact ? ' is-compact' : '')}>
      <div className="results-toolbar">
        <div className="results-summary-chips">
          {Object.entries(summary).map(([k, v]) => (
            <span className="summary-chip" key={k}>
              <small>{k.replaceAll('_', ' ')}:</small>
              <strong>{String(v)}</strong>
            </span>
          ))}
          {sections.length > 0 && (
            <span className="results-count-badge">
              {rawRows.length} {currentSection?.title || 'records'}
            </span>
          )}
          {type === 'key_value' && (
            <span className="results-count-badge">
              {Object.keys(kvData || {}).length} fields
            </span>
          )}
          {type === 'list' && (
            <span className="results-count-badge">
              {items.length} items
            </span>
          )}
        </div>

        <div className="results-actions">
          {rawRows.length > 3 && viewMode === 'table' && (
            <div className="results-filter-input">
              <Search size={13} />
              <input
                type="text"
                placeholder="Filter results…"
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
              />
              {filterText && (
                <button type="button" onClick={() => setFilterText('')}>
                  <X size={11} />
                </button>
              )}
            </div>
          )}

          <div className="view-toggle">
            <button
              type="button"
              className={'toggle-btn' + (viewMode === 'table' ? ' active' : '')}
              onClick={() => setViewMode('table')}
              title="Formatted view"
            >
              <Table size={12} /> Formatted
            </button>
            <button
              type="button"
              className={'toggle-btn' + (viewMode === 'json' ? ' active' : '')}
              onClick={() => setViewMode('json')}
              title="Raw JSON"
            >
              <Code size={12} /> JSON
            </button>
          </div>

          {rawRows.length > 0 && (
            <button
              type="button"
              className="copy-btn"
              onClick={() => downloadCSV(rawRows, `${currentSection?.title || 'extracted-data'}.csv`)}
              title="Download as CSV"
            >
              <Download size={12} />
              <span>CSV</span>
            </button>
          )}

          <button
            type="button"
            className="copy-btn"
            onClick={copyToClipboard}
            title="Copy raw JSON"
          >
            {copied ? <Check size={12} className="success-text" /> : <Copy size={12} />}
            <span>{copied ? 'Copied' : 'Copy'}</span>
          </button>
        </div>
      </div>

      {sections.length > 1 && (
        <div className="section-tabs">
          {sections.map((sec, idx) => (
            <button
              key={sec.title}
              type="button"
              className={'sec-tab' + (activeTab === idx ? ' active' : '')}
              onClick={() => {
                setActiveTab(idx);
                setFilterText('');
              }}
            >
              {sec.title} ({sec.rows?.length || 0})
            </button>
          ))}
        </div>
      )}

      <div className="results-body">
        {viewMode === 'json' ? (
          <pre className="json-preview">{JSON.stringify(raw ?? displayData, null, 2)}</pre>
        ) : type === 'text' ? (
          <div className="results-text-card">{text}</div>
        ) : type === 'list' ? (
          <ul className="results-bullet-list">
            {items.map((item, idx) => (
              <li key={idx}>
                <CellValue col="item" val={item} />
              </li>
            ))}
          </ul>
        ) : type === 'key_value' ? (
          <div className="kv-card-grid">
            {Object.entries(kvData || {}).map(([k, v]) => (
              <div className="kv-card" key={k}>
                <span className="kv-card-label">{k.replaceAll('_', ' ')}</span>
                <span className="kv-card-value">
                  <CellValue col={k} val={v} />
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className="results-table-wrap">
            <table className="results-table">
              <thead>
                <tr>
                  <th className="th-num">#</th>
                  {columns.map((col) => (
                    <th key={col}>{col.replaceAll('_', ' ')}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filteredRows.length > 0 ? (
                  filteredRows.map((row, idx) => (
                    <tr key={idx}>
                      <td className="td-num">{idx + 1}</td>
                      {columns.map((col) => (
                        <td key={col}>
                          <CellValue col={col} val={row[col]} />
                        </td>
                      ))}
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={columns.length + 1} className="no-matches">
                      No results matching "{filterText}"
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}


function ApprovalModal({ task, busy, onApprove, onScreenshotClick }) {
  const [userInput, setUserInput] = useState('');
  const approval = task?.approval_request;

  useEffect(() => setUserInput(''), [approval?.approval_id]);

  if (!approval || task?.status !== 'waiting_approval') return null;

  const labels = {
    captcha: { icon: ShieldCheck, title: 'CAPTCHA required' },
    payment: { icon: CreditCard, title: 'Payment confirmation required' },
    custom: { icon: ClipboardCheck, title: 'Agent confirmation required' },
    confirmation: { icon: CheckCircle2, title: 'Confirm action' },
  };

  const item = labels[approval.type] || {
    icon: AlertCircle,
    title: 'Human action required',
  };
  const Icon = item.icon;

  return (
    <div className="modal-backdrop">
      <section
        className="approval-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approval-title"
      >
        <header className="modal-header">
          <span className="modal-icon">
            <Icon size={22} />
          </span>
          <div>
            <h2 id="approval-title">{item.title}</h2>
            <p>Review the browser state before continuing.</p>
          </div>
        </header>

        <div className="modal-body">
          <p>{approval.prompt_message || 'The agent needs your help to continue.'}</p>

          {approval.screenshot_url && (
            <div className="screenshot-area">
              <span className="field-label">
                Current browser view <small>Click screenshot to interact</small>
              </span>
              <img
                src={api(approval.screenshot_url)}
                alt="Current browser screenshot"
                className={busy ? 'image-busy' : ''}
                onClick={onScreenshotClick}
              />
            </div>
          )}

          <label className="field-label" htmlFor="approval-user-input">
            Manual input <small>Optional — e.g. verification code</small>
          </label>
          <input
            id="approval-user-input"
            value={userInput}
            onChange={(e) => setUserInput(e.target.value)}
            placeholder="Enter input if needed"
            autoComplete="off"
          />
        </div>

        <footer className="modal-footer">
          <button
            type="button"
            className="button-secondary danger-button"
            disabled={busy}
            onClick={() => onApprove(false, userInput)}
          >
            Reject / cancel
          </button>
          <button
            type="button"
            className="button-primary"
            disabled={busy}
            onClick={() => onApprove(true, userInput)}
          >
            {busy ? (
              <LoaderCircle size={16} className="spin" />
            ) : (
              <Check size={16} />
            )}
            Approve and continue
          </button>
        </footer>
      </section>
    </div>
  );
}

function ExecutionInspector({ task }) {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!task?.task_id || !expanded) return;
    let alive = true;
    let timer = null;
    let controller = null;
    setEvents([]);
    setLoadError('');
    const load = async () => {
      controller = new AbortController();
      setLoading(true);
      try {
        const response = await fetch(
          api('/api/tasks/' + encodeURIComponent(task.task_id) + '/trace'),
          { cache: 'no-store', signal: controller.signal }
        );
        if (!response.ok) throw new Error('Trace request failed (' + response.status + ')');
        const payload = await response.json();
        if (alive) {
          setEvents(Array.isArray(payload.events) ? payload.events : []);
          setLoadError('');
        }
      } catch (err) {
        if (alive && err.name !== 'AbortError') setLoadError(err.message);
      } finally {
        if (alive) {
          setLoading(false);
          timer = window.setTimeout(load, isActive(task.status) ? 2500 : 10000);
        }
      }
    };
    load();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
      if (controller) controller.abort();
    };
  }, [task?.task_id, task?.status, expanded]);

  const ordered = [...events].reverse();
  const label = (value) => String(value || '').replaceAll('_', ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  const pretty = (value) => JSON.stringify(value ?? {}, null, 2);

  return (
    <section className="section-card" style={{ marginTop: 18, overflow: 'hidden' }}>
      <button type="button" onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, background: 'transparent', color: 'inherit', border: 0, padding: '4px 0', cursor: 'pointer', textAlign: 'left' }}>
        <span>
          <strong style={{ display: 'block', fontSize: 16 }}>Agent execution inspector</strong>
          <small style={{ color: 'var(--muted, #94a3b8)' }}>DOM snapshots · planner decisions · action results · recovery context</small>
        </span>
        <span style={{ fontSize: 13, color: 'var(--muted, #94a3b8)' }}>{expanded ? 'Hide details' : 'Inspect execution'} {events.length ? '· ' + events.length + ' events' : ''}</span>
      </button>
      {expanded && (
        <div style={{ marginTop: 18 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 12, color: 'var(--muted, #94a3b8)' }}>Live trace · latest events first · sensitive values redacted</span>
            {loading && <span style={{ fontSize: 12 }}>Refreshing…</span>}
          </div>
          {loadError && <p role="alert" style={{ color: '#ef4444' }}>{loadError}</p>}
          {!events.length && !loading ? <p style={{ color: 'var(--muted, #94a3b8)' }}>No trace events yet. Events appear as the agent inspects the page and chooses actions.</p> : (
            <div style={{ display: 'grid', gap: 10 }}>
              {ordered.map((event, index) => {
                const data = event.data || {};
                const action = data.selected_action || data.action || {};
                const target = action.element_id != null ? 'Element #' + action.element_id : (action.selector || action.url || action.text || 'No explicit target');
                return (
                  <details key={event.event_id || index} style={{ border: '1px solid var(--border, #334155)', borderRadius: 10, padding: 12, minWidth: 0 }}>
                    <summary style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                      <strong>{label(event.event_type)}</strong>
                      <span style={{ fontSize: 12, opacity: .7 }}>Step {event.step_number}</span>
                      <span style={{ fontSize: 12, opacity: .7 }}>{event.timestamp ? new Date(event.timestamp * 1000).toLocaleTimeString() : ''}</span>
                      {event.event_type === 'action_result' && <span style={{ marginLeft: 'auto', color: data.success ? '#16a34a' : '#ef4444', fontSize: 12 }}>{data.success ? 'Succeeded' : 'Failed'}</span>}
                    </summary>
                    {event.event_type === 'dom_snapshot' ? (
                      <div style={{ marginTop: 12 }}>
                        <p style={{ margin: '0 0 8px', fontSize: 13 }}>{data.title || 'Untitled page'} · {data.url || 'URL unavailable'}</p>
                        <p style={{ margin: '0 0 8px', fontSize: 12, opacity: .7 }}>{data.element_count || 0} interactive elements · {data.form_field_count || 0} form fields</p>
                        <div style={{ display: 'grid', gap: 6 }}>
                          {(data.elements || []).map((el, i) => (
                            <div key={el.index ?? i} style={{ display: 'grid', gridTemplateColumns: '64px minmax(0,1fr)', gap: 8, fontSize: 12, padding: 7, borderRadius: 6, background: 'var(--surface-muted, rgba(148,163,184,.08))' }}>
                              <strong>#{el.index ?? i} · {el.tag || el.role || 'node'}</strong>
                              <span style={{ overflowWrap: 'anywhere' }}>{el.text || el.aria_label || el.label || el.placeholder || el.title || '(unlabelled)'} {el.selector ? '· ' + el.selector : ''}</span>
                            </div>
                          ))}
                        </div>
                        {data.page_text && <details style={{ marginTop: 10 }}><summary>Captured visible page text</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>{data.page_text}</pre></details>}
                      </div>
                    ) : (
                      <div style={{ marginTop: 12 }}>
                        {event.event_type === 'planner_decision' && <p style={{ fontSize: 13 }}>Selected target: {target}</p>}
                        <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 360, overflow: 'auto', fontSize: 12, background: 'var(--surface-muted, rgba(148,163,184,.08))', padding: 10, borderRadius: 8 }}>{pretty(data)}</pre>
                      </div>
                    )}
                  </details>
                );
              })}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export default function App() {
  const [goal, setGoal] = useState('');
  const [tasks, setTasks] = useState({});
  const [currentTaskId, setCurrentTaskId] = useState(null);
  const [pool, setPool] = useState(null);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [launching, setLaunching] = useState(false);
  const [busyAction, setBusyAction] = useState(false);
  const [history, setHistory] = useState([]);
  const [showMyContext, setShowMyContext] = useState(false);
  const sockets = useRef(new Map());
  const reconnectTimers = useRef(new Map());
  const reconnectAttempts = useRef(new Map());
  const connectTaskRef = useRef(null);
  const noticeTimer = useRef(null);

  const notify = useCallback((message) => {
    setNotice(message);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(''), 4000);
  }, []);

  const closeSocket = useCallback((id) => {
    const socket = sockets.current.get(id);
    if (socket) {
      sockets.current.delete(id);
      try {
        socket.close();
      } catch {
        /* already closed */
      }
    }
  }, []);

  const scheduleReconnect = useCallback((id) => {
    if (!id || reconnectTimers.current.has(id)) return;
    const attempt = reconnectAttempts.current.get(id) || 0;
    const delay = Math.min(1000 * (2 ** attempt), 15000);
    reconnectAttempts.current.set(id, Math.min(attempt + 1, 4));
    const timer = window.setTimeout(async () => {
      reconnectTimers.current.delete(id);
      try {
        const response = await authFetch(api('/api/tasks'));
        if (!response.ok) throw new Error('Task refresh failed');
        const active = await response.json();
        const task = active.find((item) => item.task_id === id && isActive(item.status));
        if (task) connectTaskRef.current?.(id);
      } catch {
        scheduleReconnect(id);
      }
    }, delay);
    reconnectTimers.current.set(id, timer);
  }, []);

  const pollingTimers = useRef(new Map());

  const startPolling = useCallback((id) => {
    if (!id || pollingTimers.current.has(id)) return;
    const poll = async () => {
      try {
        const res = await authFetch(api('/api/tasks/' + encodeURIComponent(id)));
        if (res.ok) {
          const raw = await res.json();
          if (raw?.task_id) {
            setTasks((prev) => ({
              ...prev,
              [id]: mergeTask(prev[id], raw),
            }));
            if (isTerminal(raw.status)) {
              stopPolling(id);
              return;
            }
          }
        }
      } catch (e) {}
      if (pollingTimers.current.has(id)) {
        const timer = window.setTimeout(poll, 1500);
        pollingTimers.current.set(id, timer);
      }
    };
    const timer = window.setTimeout(poll, 1500);
    pollingTimers.current.set(id, timer);
  }, []);

  const stopPolling = useCallback((id) => {
    const timer = pollingTimers.current.get(id);
    if (timer) window.clearTimeout(timer);
    pollingTimers.current.delete(id);
  }, []);

  const connectTask = useCallback(
    (id) => {
      if (!id || sockets.current.has(id)) return;
      const oldTimer = reconnectTimers.current.get(id);
      if (oldTimer) window.clearTimeout(oldTimer);
      reconnectTimers.current.delete(id);

      (async () => {
        let ticket = '';
        try {
          const tRes = await authFetch(api('/api/auth/ws-ticket'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_id: id }),
          });
          if (tRes.ok) {
            const tData = await tRes.json();
            ticket = tData.ticket || '';
          }
        } catch (e) {}

        let socket;
        try {
          const wsEndpoint = wsUrl('/api/ws/tasks/' + encodeURIComponent(id) + (ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''));
          socket = new WebSocket(wsEndpoint);
        } catch {
          startPolling(id);
          scheduleReconnect(id);
          return;
        }
        sockets.current.set(id, socket);

        socket.onopen = () => {
          stopPolling(id);
          reconnectAttempts.current.delete(id);
        };
        socket.onmessage = (event) => {
          try {
            const raw = JSON.parse(event.data);
            if (!raw?.task_id) return;
            setTasks((prev) => ({
              ...prev,
              [id]: mergeTask(prev[id], raw),
            }));
            if (isTerminal(raw.status)) {
              reconnectAttempts.current.delete(id);
              stopPolling(id);
              const timer = reconnectTimers.current.get(id);
              if (timer) window.clearTimeout(timer);
              reconnectTimers.current.delete(id);
              window.setTimeout(() => closeSocket(id), 3000);
            }
          } catch (err) {
            console.error('Could not process task update', err);
          }
        };

        socket.onerror = () => {
          startPolling(id);
        };
        socket.onclose = () => {
          if (sockets.current.get(id) !== socket) return;
          sockets.current.delete(id);
          startPolling(id);
          scheduleReconnect(id);
        };
      })();
    },
    [closeSocket, scheduleReconnect, startPolling, stopPolling]
  );
  connectTaskRef.current = connectTask;

  const refreshTasks = useCallback(async () => {
    try {
      const response = await authFetch(api('/api/tasks'));
      if (!response.ok) {
        if (response.status === 401 || response.status === 403 || response.status === 429) {
          return;
        }
        throw new Error('Task refresh failed (' + response.status + ')');
      }
      const list = await response.json();
      (Array.isArray(list) ? list : []).forEach((raw) => {
        if (!raw?.task_id) return;
        setTasks((prev) => ({
          ...prev,
          [raw.task_id]: mergeTask(prev[raw.task_id], raw),
        }));
        setCurrentTaskId((selected) => selected || raw.task_id);
        if (isActive(raw.status)) connectTask(raw.task_id);
      });
      setError('');
    } catch (err) {
      setError(
        (prev) =>
          prev ||
          'Cannot reach FastAPI. Start the backend on port 8000. ' + err.message
      );
    }
  }, [connectTask]);

  const refreshPool = useCallback(async () => {
    try {
      const response = await authFetch(api('/api/pool/status'));
      if (!response.ok) return;
      const data = await response.json();
      setPool(data);
      (data.running_tasks || []).forEach((raw) => {
        if (!raw?.task_id) return;
        setTasks((prev) => ({
          ...prev,
          [raw.task_id]: mergeTask(prev[raw.task_id], raw),
        }));
        setCurrentTaskId((selected) => selected || raw.task_id);
        connectTask(raw.task_id);
      });
    } catch {
      /* transient status errors are non-fatal */
    }
  }, [connectTask]);

  const refreshHistory = useCallback(async () => {
    try {
      const response = await authFetch(api('/api/tasks/history'));
      if (response.ok) {
        const data = await response.json();
        setHistory(Array.isArray(data) ? data : []);
      }
    } catch {
      /* optional endpoint */
    }
  }, []);

  useEffect(() => {
    refreshTasks();
    refreshPool();
    refreshHistory();

    const timer = window.setInterval(() => {
      refreshTasks();
      refreshPool();
      refreshHistory();
    }, 3500);

    return () => {
      window.clearInterval(timer);
      if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
      sockets.current.forEach((socket) => {
        try {
          socket.close();
        } catch {
          /* no-op */
        }
      });
      sockets.current.clear();
      reconnectTimers.current.forEach((timerId) => window.clearTimeout(timerId));
      reconnectTimers.current.clear();
    };
  }, [refreshTasks, refreshPool, refreshHistory]);

  const allTasks = useMemo(() => {
    const merged = { ...tasks };
    (history || []).forEach((raw) => {
      if (!raw?.task_id) return;
      const historical = normalizeTask(raw);
      const existing = merged[raw.task_id];
      const timestamp = (task) => {
        const value = task?.updated_at;
        if (value == null) return 0;
        // API timestamps are Unix seconds (numbers), not date strings.
        if (typeof value === 'number' && Number.isFinite(value)) return value;
        const numeric = Number(value);
        if (String(value).trim() !== '' && Number.isFinite(numeric)) return numeric;
        const parsed = Date.parse(value);
        return Number.isFinite(parsed) ? parsed / 1000 : 0;
      };
      if (!existing) {
        merged[raw.task_id] = historical;
      } else if (timestamp(historical) >= timestamp(existing)) {
        merged[raw.task_id] = mergeTask(existing, historical);
      } else {
        merged[raw.task_id] = mergeTask(historical, existing);
      }
    });
    const timestamp = (task) => {
      const value = task?.updated_at;
      if (value == null) return 0;
      // API timestamps are Unix seconds (numbers), not date strings.
      if (typeof value === 'number' && Number.isFinite(value)) return value;
      const numeric = Number(value);
      if (String(value).trim() !== '' && Number.isFinite(numeric)) return numeric;
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed / 1000 : 0;
    };
    return Object.values(merged).sort(
      (a, b) =>
        Number(isActive(b.status)) - Number(isActive(a.status)) ||
        timestamp(b) - timestamp(a)
    );
  }, [tasks, history]);

  const currentTask =
    allTasks.find((task) => task.task_id === currentTaskId) ||
    tasks[currentTaskId] ||
    null;
  const activeTasks = allTasks.filter((task) => isActive(task.status));
  const visibleTabs = allTasks.filter(
    (task) => isActive(task.status) || task.task_id === currentTaskId
  );
  const steps = currentTask?.steps || [];
  const context = currentTask?.context;
  const hasExtractionAction = steps.some((step) => {
    const action = typeof step?.action === 'string' ? step.action : step?.action?.action;
    return String(action || '').trim().toLowerCase() === 'extract';
  });
  const hasCurrentTaskExtraction = hasExtractionAction && (currentTask?.extracted_data?.length || 0) > 0;
  const metadata = currentTask?.plan_metadata;
  const cache = metadata?.cache_details;

  const visitedUrls = useMemo(() => {
    if (context?.visited_urls && context.visited_urls.length > 0) {
      return context.visited_urls;
    }
    const found = [];
    (steps || []).forEach((s) => {
      if (s?.url && !found.includes(s.url)) found.push(s.url);
    });
    return found;
  }, [context?.visited_urls, steps]);

  const actionHistory = useMemo(() => {
    if (context?.action_history && context.action_history.length > 0) {
      return context.action_history;
    }
    return (steps || []).map((s) => ({
      step: s.step_number,
      action: s.action,
      description: s.description,
      success: !s.error && s.action !== 'error',
      timestamp: s.timestamp,
    }));
  }, [context?.action_history, steps]);

  const recentErrors = useMemo(() => {
    if (context?.recent_errors && context.recent_errors.length > 0) {
      return context.recent_errors;
    }
    return (steps || [])
      .filter((s) => s?.error)
      .map((s) => (typeof s.error === 'string' ? s.error : JSON.stringify(s.error)))
      .slice(-5);
  }, [context?.recent_errors, steps]);

  async function launchTask(event) {
    event.preventDefault();
    const value = goal.trim();
    if (!value || launching) return;
    setLaunching(true);
    setError('');

    try {
      const response = await authFetch(api('/api/tasks'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          goal: value,
          start_url: '',
          show_badges: false,
          max_steps: 25,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 429) {
          throw new Error('Too many requests. Please wait a moment before launching a new task.');
        }
        if (response.status === 401 || response.status === 403) {
          throw new Error('Authentication required. Session is initializing, please retry.');
        }
        throw new Error(payload.detail || 'Server returned ' + response.status);
      }

      const task = normalizeTask(payload);
      setTasks((prev) => ({
        ...prev,
        [task.task_id]: mergeTask(prev[task.task_id], task),
      }));
      setCurrentTaskId(task.task_id);
      setGoal('');
      connectTask(task.task_id);
      refreshPool();
      notify('Task launched.');
    } catch (err) {
      setError('Could not launch task: ' + err.message);
    } finally {
      setLaunching(false);
    }
  }

  async function updateTaskExecutionState(id, action) {
    setBusyAction(true);
    try {
      const response = await authFetch(
        api('/api/tasks/' + encodeURIComponent(id) + '/' + action),
        { method: 'POST' }
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(payload.detail || action + ' failed (' + response.status + ')');

      const task = normalizeTask(payload);
      setTasks((prev) => ({
        ...prev,
        [id]: { ...(prev[id] || {}), ...task },
      }));
      notify(action === 'pause' ? 'Task paused.' : 'Task resumed.');
      refreshPool();
    } catch (err) {
      setError('Could not ' + action + ' task: ' + err.message);
      refreshTasks();
    } finally {
      setBusyAction(false);
    }
  }

  async function cancelTask(id) {
    setBusyAction(true);
    try {
      const response = await authFetch(
        api('/api/tasks/' + encodeURIComponent(id) + '/cancel'),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'Task cancelled by user.' }),
        }
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(payload.detail || 'Cancel failed (' + response.status + ')');

      const task = normalizeTask(payload);
      setTasks((prev) => ({ ...prev, [id]: { ...(prev[id] || {}), ...task } }));
      notify('Cancellation requested.');
      refreshPool();
    } catch (err) {
      setError('Could not cancel task: ' + err.message);
    } finally {
      setBusyAction(false);
    }
  }

  async function cancelAllTasks() {
    setBusyAction(true);
    try {
      const response = await authFetch(api('/api/tasks/cancel-all'), {
        method: 'POST',
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(
          payload.detail || 'Cancel-all failed (' + response.status + ')'
        );

      notify(
        'Cancellation requested for ' +
        (payload.cancelled_count || 0) +
        ' task(s).'
      );
      refreshTasks();
      refreshPool();
    } catch (err) {
      setError('Could not stop tasks: ' + err.message);
    } finally {
      setBusyAction(false);
    }
  }

  async function submitApproval(approved, userInput) {
    if (!currentTask) return;
    setBusyAction(true);
    try {
      const response = await authFetch(
        api('/api/tasks/' + encodeURIComponent(currentTask.task_id) + '/approve'),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            approved: Boolean(approved),
            user_input: userInput || '',
          }),
        }
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(
          payload.detail || 'Approval failed (' + response.status + ')'
        );

      // The API acknowledgement confirms receipt, not necessarily the
      // task's next state. Refresh from the server instead of guessing locally.
      await Promise.all([refreshTasks(), refreshPool(), refreshHistory()]);
      notify(approved ? 'Approval submitted.' : 'Task rejected.');
    } catch (err) {
      setError('Could not submit approval: ' + err.message);
    } finally {
      setBusyAction(false);
    }
  }

  async function clickScreenshot(event) {
    if (!currentTask || busyAction) return;
    const image = event.currentTarget;
    const rect = image.getBoundingClientRect();
    if (!rect.width || !rect.height || !image.naturalWidth || !image.naturalHeight) return;

    // object-fit: contain may letterbox the image inside its element box.
    // Translate clicks against the actual displayed image, not the full box.
    const scale = Math.min(rect.width / image.naturalWidth, rect.height / image.naturalHeight);
    const renderedWidth = image.naturalWidth * scale;
    const renderedHeight = image.naturalHeight * scale;
    const offsetX = (rect.width - renderedWidth) / 2;
    const offsetY = (rect.height - renderedHeight) / 2;
    const localX = event.clientX - rect.left - offsetX;
    const localY = event.clientY - rect.top - offsetY;
    if (localX < 0 || localY < 0 || localX > renderedWidth || localY > renderedHeight) return;

    const x = Math.max(0, Math.min(1, localX / renderedWidth));
    const y = Math.max(0, Math.min(1, localY / renderedHeight));

    setBusyAction(true);
    try {
      const params = new URLSearchParams({ x: String(x), y: String(y) });
      const response = await authFetch(
        api(
          '/api/tasks/' +
          encodeURIComponent(currentTask.task_id) +
          '/click?' +
          params
        ),
        { method: 'POST' }
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(
          payload.detail || 'Browser click failed (' + response.status + ')'
        );

      setTasks((prev) => ({
        ...prev,
        [currentTask.task_id]: {
          ...prev[currentTask.task_id],
          approval_request: {
            ...prev[currentTask.task_id]?.approval_request,
            screenshot_url: payload.screenshot_url,
          },
        },
      }));
    } catch (err) {
      setError('Browser interaction failed: ' + err.message);
    } finally {
      setBusyAction(false);
    }
  }

  async function dismissTask(id) {
    if (isActive(tasks[id]?.status)) return;
    setTasks((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    setHistory((prev) => prev.filter((task) => task.task_id !== id));
    if (currentTaskId === id) {
      setCurrentTaskId(
        allTasks.find((task) => task.task_id !== id && isActive(task.status))
          ?.task_id || null
      );
    }
    closeSocket(id);
    try {
      await authFetch(api('/api/tasks/' + encodeURIComponent(id)), { method: 'DELETE' });
    } catch {
      /* non-fatal */
    }
  }

  async function clearFinished() {
    const finishedIds = allTasks
      .filter((task) => isTerminal(task.status) && task.task_id !== currentTaskId)
      .map((t) => t.task_id);

    setTasks((prev) => {
      const next = { ...prev };
      finishedIds.forEach((id) => delete next[id]);
      return next;
    });
    setHistory((prev) =>
      prev.filter((task) => !finishedIds.includes(task.task_id))
    );

    await Promise.allSettled(
      finishedIds.map((id) =>
        authFetch(api('/api/tasks/' + encodeURIComponent(id)), { method: 'DELETE' })
      )
    );
  }


  const currentPath = window.location.pathname.replace(/\/+$/, '') || '/';
  if (currentPath !== '/agent') {
    return <Landing onOpen={() => { window.location.href = '/agent'; }} />;
  }

  return (
    <div className="app-shell">
      <header className="navbar">
        <a className="brand" href="/" aria-label="Automata dashboard">
          <span className="brand-icon">
            <img src="/automata-mark.svg" alt="Automata" />
          </span>
          <span>
            <strong className="brand-name">AUTOMATA</strong>
            <small className="brand-subtitle">YOUR AI BROWSER ASSISTANT</small>
          </span>
        </a>

        <div className="nav-right">
          <button
            type="button"
            className={"nav-stat-chip nav-stat-button" + (showMyContext ? " is-active" : "")}
            onClick={() => setShowMyContext((prev) => !prev)}
            title="Manage My Context: persistent profile, documents, and memories"
          >
            My Context
          </button>

          <span
            className={
              'nav-stat-chip' + (pool?.extension_status?.connected ? ' is-active' : '')
            }
            title={
              pool?.extension_status?.connected
                ? `Chrome Extension Connected (${pool?.extension_status?.client_count || 1} client)`
                : 'Chrome Extension Disconnected — Please connect extension in Chrome'
            }
          >
            <span
              className="pulse-dot"
              style={{
                backgroundColor: pool?.extension_status?.connected
                  ? 'var(--success, #22c55e)'
                  : 'var(--danger, #ef4444)',
              }}
            />
            {pool?.extension_status?.connected
              ? 'Extension Connected'
              : 'Extension Disconnected'}
          </span>
          <span
            className={
              'nav-stat-chip' + (activeTasks.length ? ' is-active' : '')
            }
          >
            <span className="pulse-dot" />
            {activeTasks.length} active task
            {activeTasks.length === 1 ? '' : 's'}
          </span>
        </div>
      </header>

      <main className="main-content">
        {error && (
          <div className="alert alert-error" role="alert">
            <AlertCircle size={17} />
            <span>{formatUserFacingError(error)}</span>
            <IconButton title="Dismiss error" onClick={() => setError('')}>
              <X size={15} />
            </IconButton>
          </div>
        )}

        {notice && (
          <div className="alert alert-success" role="status">
            <CheckCircle2 size={17} />
            <span>{notice}</span>
            <IconButton
              title="Dismiss notification"
              onClick={() => setNotice('')}
            >
              <X size={15} />
            </IconButton>
          </div>
        )}

        {showMyContext ? (
          <MyContext authFetch={authFetch} api={api} onClose={() => setShowMyContext(false)} />
        ) : (
          <>
            <section className="task-hero-card">
          <div className="hero-top-row">
            <div className="hero-heading">
              <h1>What should the agent do?</h1>
              <p>
                Describe a goal and the agent will plan, execute, and report back with
                live progress.
              </p>
            </div>
            <div className="hero-badges">
              <span>
                <ShieldCheck size={15} /> Human approval for sensitive actions
              </span>
              <span>
                <Zap size={15} /> Parallel tasks supported
              </span>
            </div>
          </div>

          <form className="task-form" onSubmit={launchTask}>
            <label className="goal-input-wrap">
              <Search size={19} />
              <input
                aria-label="Task goal"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="e.g. Search for the latest AI news and summarize the top story…"
              />
            </label>
            <button
              className="button-primary launch-button"
              type="submit"
              disabled={!goal.trim() || launching}
            >
              {launching ? (
                <LoaderCircle size={17} className="spin" />
              ) : (
                <Play size={17} />
              )}
              {launching ? 'Launching…' : 'Launch agent'}
              <ChevronRight size={17} />
            </button>
          </form>
        </section>

        {visibleTabs.length > 0 && (
          <section className="task-switcher">
            <div className="switcher-left">
              <div className="switcher-label">
                <span>Tasks</span>
              </div>
              <div className="task-tabs">
                {visibleTabs.map((task) => (
                  <TaskTab
                    key={task.task_id}
                    task={task}
                    selected={task.task_id === currentTaskId}
                    onSelect={() => setCurrentTaskId(task.task_id)}
                    onPause={() => updateTaskExecutionState(task.task_id, 'pause')}
                    onResume={() => updateTaskExecutionState(task.task_id, 'resume')}
                    onCancel={() => cancelTask(task.task_id)}
                    onDismiss={() => dismissTask(task.task_id)}
                  />
                ))}
              </div>
            </div>

            <div className="switcher-actions">
              <button
                className="button-secondary"
                type="button"
                onClick={() => {
                  refreshTasks();
                  refreshPool();
                  refreshHistory();
                }}
                title="Refresh tasks"
              >
                <RefreshCw size={13} /> Refresh
              </button>
              {activeTasks.length > 0 && (
                <button
                  className="button-secondary danger-button"
                  type="button"
                  onClick={cancelAllTasks}
                  disabled={busyAction}
                  title="Stop all active tasks"
                >
                  <Square size={11} /> Stop all
                </button>
              )}
              {allTasks.some((task) => isTerminal(task.status)) && (
                <button
                  className="button-secondary"
                  type="button"
                  onClick={clearFinished}
                  title="Clear finished tasks"
                >
                  <Trash2 size={13} /> Clear finished
                </button>
              )}
            </div>
          </section>
        )}

        {!currentTask ? (
          <section className="welcome-state">
            <div className="welcome-panel">
              <div className="welcome-copy">
                <span className="welcome-eyebrow"><Zap size={13} /> YOUR BROWSER, WORKING FOR YOU</span>
                <h2>Give your browser<br /><em>a little direction.</em></h2>
                <p>Automata can research websites, gather useful details, and handle repetitive browser steps. Tell it what you need above, then stay in control as it works.</p>
                <div className="welcome-assurance"><ShieldCheck size={15} /> You stay in control of important actions.</div>
              </div>
              <div className="welcome-starters">
                <div className="welcome-starters-heading">
                  <span>GET STARTED</span>
                  <small>Choose an example to fill your task</small>
                </div>
                <button type="button" className="starter-card" onClick={() => { setGoal('Research three project management tools and compare their key features and pricing.'); document.querySelector('[aria-label="Task goal"]')?.focus(); }}>
                  <span className="starter-icon"><Search size={17} /></span>
                  <span className="starter-copy"><strong>Research & compare</strong><small>Find information across websites and compare options.</small></span>
                  <ChevronRight size={16} className="starter-arrow" />
                </button>
                <button type="button" className="starter-card" onClick={() => { setGoal('Find the official website, contact details, and key information for a company.'); document.querySelector('[aria-label="Task goal"]')?.focus(); }}>
                  <span className="starter-icon"><Globe size={17} /></span>
                  <span className="starter-copy"><strong>Explore a website</strong><small>Navigate a site and collect the details that matter.</small></span>
                  <ChevronRight size={16} className="starter-arrow" />
                </button>
                <button type="button" className="starter-card" onClick={() => { setGoal('Open a web form and fill it using the details I provide. Do not submit it without my approval.'); document.querySelector('[aria-label="Task goal"]')?.focus(); }}>
                  <span className="starter-icon"><FileText size={17} /></span>
                  <span className="starter-copy"><strong>Handle a routine task</strong><small>Fill in forms and review before anything is submitted.</small></span>
                  <ChevronRight size={16} className="starter-arrow" />
                </button>
              </div>
            </div>
          </section>
        ) : (
          <>
            <section className="section-card plan-section">
              <div className="section-heading">
                <div className="section-icon plan-icon">
                  <Layers size={18} />
                </div>
                <div className="section-heading-copy">
                  <h2>Execution plan</h2>
                  <p>{currentTask.goal}</p>
                </div>
                <StatusPill task={currentTask} />
                <ReportDownloadButton task={currentTask} />
              </div>

              {metadata && (
                <div className="plan-meta">
                  <span className="meta-chip">
                    <Target size={13} />
                    Intent:{' '}
                    {metadata.category_label ||
                      metadata.category_key ||
                      'Automation'}
                  </span>
                  {cache && (
                    <span
                      className={
                        'meta-chip' + (cache.hit ? ' meta-success' : '')
                      }
                    >
                      <Database size={13} />
                      Redis plan {cache.hit ? 'hit' : 'miss'}
                      {cache.duration_ms != null
                        ? ' · ' + formatDuration(cache.duration_ms)
                        : ''}
                    </span>
                  )}
                </div>
              )}

              {(() => {
                const planSteps = extractPlanSteps(currentTask);
                return planSteps.length > 0 ? (
                  <ol className="plan-list">
                    {planSteps.map((step, index) => {
                      const activeIndex = Number(
                        currentTask.current_plan_step || 0
                      );
                      const isFailed =
                        currentTask.status === 'failed' && index === activeIndex;
                      const isCompletedTask =
                        String(currentTask.status || '').toLowerCase() === 'completed';
                      // Milestones prior to the active/failed one were completed
                      const done = isCompletedTask || index < activeIndex;
                      const active =
                        !done &&
                        !isFailed &&
                        index === activeIndex &&
                        (currentTask.status === 'running' ||
                          currentTask.plan_status === 'executing' ||
                          currentTask.plan_status === 'planning');
                      const stepText = typeof step === 'string' ? step : String(step);
                      const Icon = milestoneIcon(stepText);

                      return (
                        <li
                          className={
                            'plan-step' +
                            (done ? ' step-done' : '') +
                            (active ? ' step-active' : '') +
                            (isFailed ? ' step-failed' : '')
                          }
                          key={index + '-' + stepText}
                        >
                          <span className="milestone-icon">
                            {done ? (
                              <Check size={15} />
                            ) : isFailed ? (
                              <X size={15} />
                            ) : active ? (
                              <LoaderCircle className="spin" size={15} />
                            ) : (
                              <Icon size={15} />
                            )}
                          </span>
                          <span>{stepText}</span>
                          {done && <CheckCircle2 className="plan-check" size={15} />}
                          {isFailed && <AlertCircle className="failed-text" size={15} />}
                        </li>
                      );
                    })}
                  </ol>
                ) : (
                  <div className="plan-pending">
                    <LoaderCircle
                      size={16}
                      className={
                        currentTask.status === 'running' ? 'spin' : ''
                      }
                    />
                    <span>
                      {currentTask.status === 'failed'
                        ? 'The task failed before a plan was available.'
                        : currentTask.status === 'cancelled'
                          ? 'Task cancelled before a plan was available.'
                          : 'Generating the execution plan…'}
                    </span>
                  </div>
                );
              })()}

              {currentTask.error &&
                String(currentTask.status || '').toLowerCase() !== 'completed' &&
                String(currentTask.status || '').toLowerCase() !== 'waiting_approval' &&
                !/approval received|recovering session|waiting for the browser session/i.test(currentTask.error) && (
                <div
                  className={
                    'task-error' +
                    (/approval|waiting|recovering|human|paused|notice/i.test(currentTask.error)
                      ? ' is-warning'
                      : '')
                  }
                >
                  <AlertCircle size={15} />
                  {formatUserFacingError(currentTask.error)}
                </div>
              )}

              {currentTask.status === 'waiting_approval' && (
                <div className="approval-banner">
                  <ShieldCheck size={17} />
                  <div>
                    <strong>Human action required</strong>
                    <span>
                      {currentTask.approval_request?.prompt_message ||
                        'Review the request in the approval dialog.'}
                    </span>
                  </div>
                </div>
              )}
            </section>

            {currentTask.steps?.some((step) => {
              const action = typeof step?.action === 'string' ? step.action : step?.action?.action;
              return String(action || '').toLowerCase() === 'extract';
            }) && currentTask.extracted_data?.length > 0 && (
                <section className="section-card extracted-section">
                  <div className="section-heading">
                    <div className="section-icon results-icon">
                      <Table size={18} />
                    </div>
                    <div className="section-heading-copy">
                      <h2>Extracted results</h2>
                      <p>Structured data collected from the page</p>
                    </div>
                  </div>
                  <ExtractedResultsViewer data={currentTask.extracted_data} />
                </section>
              )}

            <div className="live-grid">
              <section className="section-card">
                <div className="section-heading">
                  <div className="section-icon action-icon">
                    <Activity size={18} />
                  </div>
                  <div className="section-heading-copy">
                    <h2>Performed actions</h2>
                    <p>Live execution log</p>
                  </div>
                  <span className="count-badge">
                    {steps.length} step{steps.length === 1 ? '' : 's'}
                  </span>
                </div>

                <div className="action-list">
                  {steps.length ? (
                    [...steps]
                      .reverse()
                      .map((step, index) => (
                        <ActionCard
                          key={step.step_number ?? index}
                          step={step}
                        />
                      ))
                  ) : (
                    <EmptyState
                      icon={Activity}
                      title="No actions yet"
                      description={
                        isActive(currentTask.status)
                          ? 'Actions appear as the agent executes the plan.'
                          : 'This task has no recorded actions.'
                      }
                    />
                  )}
                </div>
              </section>

              <section className="section-card context-panel">
                <div className="section-heading">
                  <div className="section-icon memory-icon">
                    <Database size={18} />
                  </div>
                  <div className="section-heading-copy">
                    <h2>Context memory</h2>
                    <p>Task context and state</p>
                  </div>
                </div>

                {cache && (
                  <div className="cache-summary-compact">
                    <span
                      className={
                        'status-tag ' + (cache.hit ? 'status-ok' : 'status-warn')
                      }
                    >
                      Plan cache {cache.hit ? 'hit' : 'miss'}
                    </span>
                    <span className="cache-summary-text">
                      {cache.message ||
                        (cache.hit
                          ? 'Reused cached execution plan'
                          : 'Generated fresh plan')}
                    </span>
                    {cache.duration_ms != null && (
                      <span className="cache-summary-duration">
                        {formatDuration(cache.duration_ms)}
                      </span>
                    )}
                  </div>
                )}

                <div className="context-group">
                  <h3>Visited URLs</h3>
                  {visitedUrls?.length ? (
                    <ul className="context-list">
                      {visitedUrls.slice(-12).map((url) => (
                        <li key={url}>
                          {isSafeWebUrl(url) ? (
                            <a href={url} target="_blank" rel="noopener noreferrer">
                              {url}
                            </a>
                          ) : (
                            <span className="muted-text" title="Untrusted or non-navigable URL scheme">{url}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="muted-text">No visited URLs recorded yet.</p>
                  )}
                </div>

                <div className="context-group">
                  <h3>Recent actions</h3>
                  {actionHistory?.length ? (
                    <ul className="context-list">
                      {actionHistory
                        .slice(-8)
                        .reverse()
                        .map((item, index) => (
                          <li key={(item.step ?? index) + '-' + index}>
                            <span
                              className={
                                item.success === false
                                  ? 'failed-text'
                                  : 'success-text'
                              }
                            >
                              {item.success === false ? 'Failed' : 'OK'}
                            </span>{' '}
                            · {item.description}
                          </li>
                        ))}
                    </ul>
                  ) : (
                    <p className="muted-text">
                      Task context appears here during execution.
                    </p>
                  )}
                </div>

                {context?.filled_fields &&
                  Object.keys(context.filled_fields).length > 0 && (
                    <div className="context-group">
                      <h3>Filled form fields</h3>
                      <div className="field-grid">
                        {Object.entries(context.filled_fields).map(
                          ([selector, value]) => {
                            const label =
                              typeof value === 'object' && value !== null
                                ? value.label || selector
                                : selector;
                            const val =
                              typeof value === 'object' && value !== null
                                ? value.value ?? ''
                                : value;
                            return (
                              <div className="field-item" key={selector}>
                                <small>{label}</small>
                                <strong>{String(val ?? '')}</strong>
                              </div>
                            );
                          }
                        )}
                      </div>
                    </div>
                  )}

                {recentErrors?.length > 0 && (
                  <div className="context-group">
                    <h3>Recent errors</h3>
                    <ul className="context-list error-list">
                      {recentErrors.map((item, index) => (
                        <li key={index}>
                          {typeof item === 'object' && item !== null
                            ? item.error || item.message || JSON.stringify(item)
                            : String(item)}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {hasCurrentTaskExtraction && context?.extracted_snippets?.length > 0 && (
                  <div className="context-group">
                    <h3>Latest extracted snippet</h3>
                    <ExtractedResultsViewer
                      data={
                        context.extracted_snippets[
                        context.extracted_snippets.length - 1
                        ]
                      }
                      compact
                    />
                  </div>
                )}
              </section>
            </div>
          </>
        )}
          </>
        )}
      </main>

      <footer className="app-footer">
        <span>Automata</span>
        <span>
          {pool?.extension_status?.connected
            ? `Chrome Extension connected (${pool?.extension_status?.client_count || 1} client)`
            : 'Chrome Extension disconnected'}
        </span>
      </footer>

      <ApprovalModal
        task={currentTask}
        busy={busyAction}
        onApprove={submitApproval}
        onScreenshotClick={clickScreenshot}
      />
    </div>
  );
}
