'use client';

import Script from 'next/script';
import { Fragment, useEffect, useRef, useState, type FormEvent } from 'react';
import { upload } from '@vercel/blob/client';
import { ArrowLeft, ArrowRight, ChevronDown, Download, FileArchive, Link2, LoaderCircle, RefreshCw, Trash2, Upload, X } from 'lucide-react';
import type { Submission } from '@/lib/submissions';
import { formatLatency, submissionMetrics } from '@/lib/results';
import officialResults from '@/content/official-results.json';

declare global {
  interface Window {
    turnstile?: {
      render: (element: HTMLElement, options: { sitekey: string; action: string; size: string; appearance: string;
        callback: (token: string) => void; 'expired-callback': () => void; 'error-callback': () => void }) => string;
      remove: (id: string) => void;
    };
  }
}

function BotCheck({ siteKey, onToken }: { siteKey: string; onToken: (token: string) => void }) {
  const element = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState('Checking browser...');
  useEffect(() => {
    if (!ready || !element.current || !window.turnstile) return;
    const widget = window.turnstile.render(element.current, { sitekey: siteKey, action: 'submit-run',
      size: 'flexible', appearance: 'interaction-only',
      callback: token => { onToken(token); setStatus(''); },
      'expired-callback': () => { onToken(''); setStatus('Refreshing browser check...'); },
      'error-callback': () => { onToken(''); setStatus('Browser check failed. Reload the page to retry.'); },
    });
    return () => { window.turnstile?.remove(widget); };
  }, [ready, siteKey, onToken]);
  return <div className="min-w-[300px] max-w-md max-[339px]:-mx-1.5">
    <Script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" onReady={() => setReady(true)}
      onError={() => setStatus('Could not load the bot check. Reload the page to retry.')} />
    <div ref={element} />
    {status && <p role="status" className="text-xs text-muted">{status}</p>}
  </div>;
}

export function SubmissionReceipt() {
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const match = /^#([0-9a-f-]{36})\.([\w-]{43})$/i.exec(window.location.hash);
    setReceipt(match ? { id: match[1], token: match[2] } : null);
    setLoaded(true);
  }, []);
  if (!loaded) return <p role="status">Loading receipt...</p>;
  if (!receipt) return <p role="alert">This receipt link is incomplete or invalid.</p>;
  return <Submissions mode="receipt" receipt={receipt} />;
}

type Mode = 'mine' | 'public' | 'admin' | 'receipt';
type Receipt = { id: string; token: string };
const inputClass = 'field mt-1 w-full min-w-0';
const buttonClass = 'command justify-center disabled:cursor-not-allowed disabled:opacity-50';
const labels: Record<string, string> = { uploading: 'Awaiting upload', queued: 'Queued', validating: 'Checking evidence',
  accepted: 'Self-submitted · Unverified', rejected: 'Rejected', removed: 'Removed' };

async function api(url: string, method = 'GET', body?: unknown, signal?: AbortSignal, token?: string) {
  const response = await fetch(url, { method, cache: 'no-store', signal,
    headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}

function SubmissionDetails({ row }: { row: Submission }) {
  if (!row.summary) return null;
  return <div className="mt-3 text-sm">
    <dl className="grid grid-cols-3 gap-3">{Object.entries(row.summary.passes_by_persona).map(([persona, count]) => <div key={persona}><dt className="capitalize text-muted">{persona}</dt><dd>{count}/200</dd></div>)}</dl>
    <p className="mt-3 text-xs text-muted">Cost includes agent and memory-system processing during ingestion and testing.</p>
    {submissionMetrics(row.summary).totalCost === null && <p className="mt-3 text-sm text-fail">
      Cost reporting is incomplete. Collect the missing phase totals and repackage the saved run before submitting again.
    </p>}
    <p className="mt-3 break-all text-xs text-muted">ZIP SHA-256: {row.blob_sha256}</p>
    <p className="mt-1 break-all text-xs text-muted">Release SHA-256: {row.release_sha256}</p>
    <a className="mt-3 text-link inline-flex items-center gap-2" download={`${row.id}.json`}
      href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify({ id: row.id, name: row.name, harness: row.harness, model: row.model, memory: row.memory, verification: 'self-submitted-unverified', archive_sha256: row.blob_sha256, release_sha256: row.release_sha256, validator_sha256: row.validator_sha256, summary: row.summary }, null, 2))}`}><Download size={14} />Download result</a>
  </div>;
}

function SubmissionTable({ rows }: { rows: Submission[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  rows = rows.filter(row => submissionMetrics(row.summary).totalCost !== null);
  return <div className="results-table-wrap mt-4 overflow-x-auto" tabIndex={0} role="region" aria-label="Self-submitted results table">
    <table className="results-table submission-results-table" aria-label="Self-submitted results">
      <colgroup>
        <col style={{ width: '18%' }} />
        <col style={{ width: '12%' }} /><col style={{ width: '12%' }} /><col style={{ width: '12%' }} />
        <col style={{ width: '11.5%' }} /><col style={{ width: '11.5%' }} /><col style={{ width: '11.5%' }} /><col style={{ width: '11.5%' }} />
      </colgroup>
      <thead><tr>
        <th scope="col">Run</th><th scope="col">Memory</th><th scope="col">Model</th><th scope="col">Harness</th>
        <th scope="col" className="text-right">Accuracy</th>
        <th scope="col" className="text-right">Total cost</th>
        <th scope="col" className="text-right">Median latency</th>
        <th scope="col" className="text-right">p95 latency</th>
      </tr></thead>
      <tbody>{rows.map(row => {
        const metrics = submissionMetrics(row.summary);
        return <Fragment key={row.id}>
          <tr>
            <th scope="row" className="text-left font-medium">
              <div className="flex items-start justify-between gap-2"><span className="min-w-0">{row.name}</span>
                <button type="button" className="result-expand"
                  title="Score details" aria-label={`Score details for ${row.name}`}
                  aria-expanded={expanded === row.id} aria-controls={`submission-${row.id}`}
                  onClick={() => setExpanded(expanded === row.id ? null : row.id)}><ChevronDown size={16} aria-hidden="true" /></button>
              </div>
              <span className="mt-1 block text-xs font-normal text-muted">Self-submitted · Unverified</span>
              {officialResults.release_sha256 && row.release_sha256 !== officialResults.release_sha256 &&
                <span className="mt-1 block text-xs font-normal text-muted">Different dataset version</span>}
            </th>
            <td>{row.memory}</td><td>{row.model}</td><td>{row.harness}</td>
            <td className="submission-accuracy text-right tabular-nums">
              {row.summary ? <><strong>{(100 * row.summary.passes / row.summary.tests).toFixed(1)}%</strong><small>{row.summary.passes} / {row.summary.tests}</small></> : 'Unavailable'}
            </td>
            <td className="text-right font-mono tabular-nums">{metrics.totalCost === null ? 'Incomplete' : `$${metrics.totalCost.toFixed(2)}`}</td>
            <td className="text-right font-mono tabular-nums">{formatLatency(metrics.medianLatency)}</td>
            <td className="text-right font-mono tabular-nums">{formatLatency(metrics.p95Latency)}</td>
          </tr>
          {expanded === row.id && <tr id={`submission-${row.id}`}><td colSpan={8}><SubmissionDetails row={row} /></td></tr>}
        </Fragment>;
      })}</tbody>
    </table>
  </div>;
}

export default function Submissions({ mode = 'mine', receipt }: { mode?: Mode; receipt?: Receipt }) {
  const [rows, setRows] = useState<Submission[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [removing, setRemoving] = useState<Submission | null>(null);
  const [reason, setReason] = useState('');
  const [actionId, setActionId] = useState('');
  const [maxBytes, setMaxBytes] = useState(256 * 1024 * 1024);
  const [siteKey, setSiteKey] = useState('');
  const [botToken, setBotToken] = useState('');
  const [botVersion, setBotVersion] = useState(0);
  const [latestReceipt, setLatestReceipt] = useState<Receipt | null>(null);
  const [archive, setArchive] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const archiveInput = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const abort = new AbortController();
    let sequence = 0;
    async function load() {
      const current = ++sequence;
      try {
        const result = receipt
          ? { enabled: true, hasMore: false, submissions: [await api(`/api/submissions/${receipt.id}/`, 'GET', undefined, abort.signal, receipt.token)] }
          : await api(`/api/submissions/?view=${mode}&page=${page}`, 'GET', undefined, abort.signal);
        if (current !== sequence || abort.signal.aborted) return;
        setRows(result.submissions); setHasMore(result.hasMore); setEnabled(result.enabled);
        setLoadError('');
        if (result.maxUploadBytes) setMaxBytes(result.maxUploadBytes);
        if (result.siteKey) setSiteKey(result.siteKey);
        setLoading(false);
      } catch (failure) {
        if (!abort.signal.aborted && current === sequence) {
          setLoadError(failure instanceof Error ? failure.message : 'Could not load submissions.'); setLoading(false);
        }
      }
    }
    void load();
    const timer = setInterval(() => { if (document.visibilityState === 'visible') void load(); }, mode === 'public' ? 30_000 : 8_000);
    return () => { abort.abort(); clearInterval(timer); };
  }, [mode, page, refresh, receipt?.id, receipt?.token]);

  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => { if (removing) dialog.current?.showModal(); else dialog.current?.close(); }, [removing]);

  function selectArchive(files: FileList | null) {
    if (!enabled || busy || !files?.length) return;
    const file = files[0];
    if (files.length !== 1 || !file.size || !file.name.toLowerCase().endsWith('.zip') || file.size > maxBytes) {
      setError(`Choose one ZIP between 1 byte and ${maxBytes / 1024 ** 2} MiB.`);
      setArchive(null);
      if (archiveInput.current) archiveInput.current.value = '';
      return;
    }
    const selection = new DataTransfer();
    selection.items.add(file);
    if (archiveInput.current) archiveInput.current.files = selection.files;
    setArchive(file); setError('');
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const file = data.get('archive');
    if (!botToken) { setError('Complete the bot check before submitting.'); return; }
    if (!(file instanceof File) || !file.size || !file.name.toLowerCase().endsWith('.zip') || file.size > maxBytes) {
      setError(`Choose a ZIP between 1 byte and ${maxBytes / 1024 ** 2} MiB.`); return;
    }
    setBusy(true); setError(''); setNotice(''); setProgress(0);
    const abort = new AbortController(); controller.current = abort;
    try {
      const reservation = await api('/api/submissions/', 'POST', {
        name: data.get('name'), harness: data.get('harness'), model: data.get('model'), memory: data.get('memory'),
        filename: file.name, size: file.size, confirm: data.get('confirm') === 'on',
        contact_email: data.get('contact_email'), botToken,
      }, abort.signal);
      setLatestReceipt({ id: reservation.id, token: reservation.receipt });
      await upload(reservation.pathname, file, { access: 'private', contentType: 'application/zip',
        handleUploadUrl: '/api/submissions/upload/', clientPayload: JSON.stringify({ id: reservation.id, receipt: reservation.receipt }), multipart: true,
        abortSignal: abort.signal, onUploadProgress: ({ percentage }) => setProgress(percentage) });
      setNotice('Upload complete. Queuing the evidence check.');
      await api(`/api/submissions/${reservation.id}/`, 'POST', undefined, undefined, reservation.receipt);
      setNotice('Submitted. The score will appear automatically if the evidence passes validation.');
      form.reset(); setArchive(null); setPage(0);
    } catch (failure) {
      setError(abort.signal.aborted ? 'Upload stopped. Any completed upload appears below; use Check upload to recover it.'
        : failure instanceof Error ? failure.message : 'Upload failed.');
    } finally { setBusy(false); setBotToken(''); setBotVersion(value => value + 1); controller.current = null; setRefresh(value => value + 1); }
  }

  async function action(row: Submission, command: 'complete' | 'retry' | 'remove' | 'withdraw') {
    setActionId(row.id); setError('');
    try {
      await api(`/api/submissions/${row.id}/`, command === 'complete' ? 'POST' : 'PATCH',
        command === 'complete' ? undefined : { action: command, reason }, undefined, row.receipt);
      setRemoving(null); setReason(''); setRefresh(value => value + 1);
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Request failed.'); }
    finally { setActionId(''); }
  }

  const visibleError = error || loadError;
  return (
    <div className="min-w-0">
      {mode === 'mine' && <form onSubmit={submit} className="space-y-4">
        <fieldset disabled={!enabled || busy} className="grid min-w-0 gap-4 sm:grid-cols-2 disabled:opacity-60">
          {[['name', 'Run name', 120], ['harness', 'Harness', 120], ['model', 'Model', 200], ['memory', 'Memory system', 120]].map(([name, label, maximum]) => (
            <label key={name} className="min-w-0 text-sm font-medium">{label}
              <input name={String(name)} required maxLength={Number(maximum)} className={inputClass} />
            </label>
          ))}
          <label className="min-w-0 text-sm font-medium sm:col-span-2">Contact email (optional)
            <input name="contact_email" type="email" maxLength={254} autoComplete="email" className={inputClass} />
          </label>
          <div className="min-w-0 sm:col-span-2">
            <span className="text-sm font-medium">Submission ZIP</span>
            <label className={`relative mt-2 flex h-48 min-w-0 flex-col items-center justify-center rounded-lg border-2 border-dotted px-5 text-center transition-colors focus-within:ring-2 focus-within:ring-ink focus-within:ring-offset-2 ${dragging ? 'border-ink bg-sand' : 'border-control bg-white hover:border-ink hover:bg-sand/40'}`}
              onDragEnter={event => { event.preventDefault(); if (enabled && !busy) { dragDepth.current++; setDragging(true); } }}
              onDragLeave={event => { event.preventDefault(); dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragging(false); }}
              onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = enabled && !busy ? 'copy' : 'none'; }}
              onDrop={event => { event.preventDefault(); dragDepth.current = 0; setDragging(false); selectArchive(event.dataTransfer.files); }}>
              <input ref={archiveInput} name="archive" type="file" aria-label="Submission ZIP" accept=".zip,application/zip" required
                onChange={event => selectArchive(event.currentTarget.files)}
                className="absolute inset-0 h-full w-full cursor-pointer opacity-0 disabled:cursor-not-allowed" />
              <span className="pointer-events-none flex w-full min-w-0 flex-col items-center gap-3" aria-live="polite">
                {archive ? <FileArchive size={28} className="text-ink" aria-hidden="true" /> : <Upload size={28} className={dragging ? 'text-ink' : 'text-muted'} aria-hidden="true" />}
                <span className="line-clamp-2 w-full break-all text-sm font-medium text-ink" title={archive?.name}>
                  {dragging ? 'Drop your ZIP here' : archive ? archive.name : 'Drag and drop your ZIP here'}
                </span>
                <span className="text-sm text-muted">{archive ? 'Click or drop another ZIP to replace' : <>or <span className="text-ink underline underline-offset-4">browse files</span></>}</span>
                <span className="text-xs text-muted">{archive ? `${archive.size < 1024 ? `${archive.size} B` : archive.size < 1024 ** 2 ? `${(archive.size / 1024).toFixed(1)} KiB` : `${(archive.size / 1024 ** 2).toFixed(2)} MiB`} selected` : `ZIP only, up to ${maxBytes / 1024 ** 2} MiB`}</span>
              </span>
            </label>
            <div className="mt-2 flex min-h-6 items-start justify-between gap-3">
              <span className="text-xs text-muted">ingestion.json and tests.json only.</span>
              {archive && <button type="button" aria-label="Remove selected ZIP" title="Remove selected ZIP" className="shrink-0 rounded p-1 text-muted hover:bg-white hover:text-ink disabled:opacity-50"
                onClick={() => { setArchive(null); if (archiveInput.current) archiveInput.current.value = ''; }}><X size={16} aria-hidden="true" /></button>}
            </div>
          </div>
          <label className="flex items-start gap-2 text-sm font-normal sm:col-span-2">
            <input name="confirm" type="checkbox" required className="mt-1 shrink-0" />
            <span>These are my recorded run results. I consent to publishing the run name, configuration, and scores as self-submitted and unverified.</span>
          </label>
        </fieldset>
        {enabled && siteKey && <BotCheck key={botVersion} siteKey={siteKey} onToken={setBotToken} />}
        <div className="flex flex-wrap items-center gap-3">
          <button type="submit" disabled={!enabled || busy || !botToken} className={`${buttonClass} bg-ink text-paper hover:bg-hover-ink`}>
            {busy ? <LoaderCircle size={16} className="animate-spin" aria-hidden="true" /> : <Upload size={16} aria-hidden="true" />}
            {busy ? 'Uploading' : 'Submit run'}
          </button>
          {busy && <button type="button" className={buttonClass} onClick={() => controller.current?.abort()}><X size={16} />Stop upload</button>}
          {!enabled && !loading && !visibleError && <span className="text-sm text-muted">Uploads are not open yet.</span>}
        </div>
        {busy && <div className="flex items-center gap-3 text-sm"><progress aria-label="Upload progress" value={progress} max={100} className="h-2 w-full" /><span className="w-12 shrink-0 text-right">{Math.round(progress)}%</span></div>}
      </form>}

      {notice && <p role="status" className="mt-4 text-sm text-muted">{notice}</p>}
      {visibleError && <p role="alert" className="mt-4 break-words text-sm text-fail">{visibleError}</p>}
      {latestReceipt && <p className="mt-3 text-sm"><a data-private="true" className="inline-flex items-center gap-2 underline" href={`/run/receipt/#${latestReceipt.id}.${latestReceipt.token}`}><Link2 size={14} />Private receipt</a></p>}

      <div className="mt-8 flex items-center justify-between gap-3">
        <h3 className="text-lg font-semibold">{mode === 'mine' ? 'Submissions from this browser' : mode === 'admin' ? 'Manage submissions' : mode === 'receipt' ? 'Submission status' : 'Self-submitted runs'}</h3>
        <button type="button" className={buttonClass} aria-label="Refresh submissions" title="Refresh submissions" onClick={() => { setError(''); setRefresh(value => value + 1); }}><RefreshCw size={16} /></button>
      </div>
      {loading ? <p role="status" className="py-5 text-sm text-muted">Loading submissions...</p>
        : rows.length === 0 ? <p className="py-5 text-sm text-muted">{mode === 'public' ? 'No self-submitted runs yet.' : 'No submissions yet.'}</p>
        : mode === 'public' ? <SubmissionTable rows={rows} />
        : <div className="mt-4 divide-y divide-hairline border-y border-hairline">
          {rows.map(row => <article key={row.id} className="min-w-0 py-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <h4 className="break-words text-base font-semibold">{row.name}</h4>
                <p className="mt-1 break-words text-sm text-muted">{row.harness} / {row.model} / {row.memory}</p>
                <p className="mt-2 text-xs text-muted">{labels[row.status] || row.status}{row.status === 'queued' && row.attempts >= 3 ? ' · Administrator attention needed' : ''}</p>
              </div>
              {row.summary && <div className="shrink-0 text-right text-base font-semibold">{row.summary.passes}/{row.summary.tests}<span className="mt-1 block text-sm font-normal text-muted">{(100 * row.summary.passes / row.summary.tests).toFixed(1)}%</span></div>}
            </div>
            {row.error_message && <p className="mt-3 break-words text-sm text-fail">{row.error_message}</p>}
            {row.removal_reason && <p className="mt-3 break-words text-sm text-muted">Removal reason: {row.removal_reason}</p>}
            {mode === 'admin' && row.contact_email && <p className="mt-3 break-words text-sm text-muted">Contact: {row.contact_email}</p>}
            {row.summary && <details className="mt-3 text-sm">
              <summary className="cursor-pointer text-muted">Score details</summary>
              <SubmissionDetails row={row} />
            </details>}
            <div className="mt-3 flex flex-wrap gap-2">
              {(mode === 'mine' || mode === 'receipt') && row.receipt && <>
                {mode === 'mine' && <a data-private="true" className={buttonClass} href={`/run/receipt/#${row.id}.${row.receipt}`}><Link2 size={14} />Private receipt</a>}
                {row.status === 'uploading' && <button className={buttonClass} disabled={actionId === row.id} onClick={() => void action(row, 'complete')}><RefreshCw size={14} />Check upload</button>}
                {row.status !== 'removed' && <button className={buttonClass} onClick={() => { setError(''); setRemoving(row); }}><X size={14} />Withdraw</button>}
              </>}
              {mode === 'admin' && row.status === 'queued' && <button className={buttonClass} disabled={actionId === row.id} onClick={() => void action(row, 'retry')}><RefreshCw size={14} />Retry validation</button>}
              {mode === 'admin' && row.status !== 'removed' && <button className={buttonClass} onClick={() => { setError(''); setReason(''); setRemoving(row); }}><Trash2 size={14} />Remove</button>}
            </div>
          </article>)}
        </div>}
      {(page > 0 || hasMore) && <div className="mt-4 flex items-center justify-between gap-3">
        <button className={buttonClass} disabled={page === 0} onClick={() => setPage(value => value - 1)} aria-label="Previous submissions" title="Previous submissions"><ArrowLeft size={16} /></button>
        <span className="text-sm text-muted">Page {page + 1}</span>
        <button className={buttonClass} disabled={!hasMore} onClick={() => setPage(value => value + 1)} aria-label="Next submissions" title="Next submissions"><ArrowRight size={16} /></button>
      </div>}
      <dialog ref={dialog} onCancel={() => setRemoving(null)} className="w-[calc(100%_-_2rem)] max-w-md rounded-lg border border-hairline bg-white p-6 backdrop:bg-black/40">
        <form onSubmit={event => { event.preventDefault(); if (removing) void action(removing, mode === 'admin' ? 'remove' : 'withdraw'); }}>
          <h3 className="break-words text-lg font-semibold">{mode === 'admin' ? 'Remove' : 'Withdraw'} {removing?.name}?</h3>
          {mode === 'admin' ? <label className="mt-4 block text-sm">Reason<textarea required maxLength={500} value={reason} onChange={event => setReason(event.target.value)} className="mt-1 w-full min-w-0 rounded-md border border-control bg-paper px-3 py-2 text-sm text-ink" rows={3} /></label>
            : <p className="mt-4 text-sm text-muted">This removes the run from the public results and stops any pending validation.</p>}
          {error && <p role="alert" className="mt-3 break-words text-sm text-fail">{error}</p>}
          <div className="mt-4 flex justify-end gap-2"><button type="button" className={buttonClass} onClick={() => setRemoving(null)}>Cancel</button><button type="submit" className={buttonClass} disabled={Boolean(actionId)}><Trash2 size={14} />{mode === 'admin' ? 'Remove run' : 'Withdraw run'}</button></div>
        </form>
      </dialog>
    </div>
  );
}
