"use client";

import { useEffect, useRef, useState } from "react";

export type WorkflowPolicy = { mode: "review" | "autonomous"; requested_mode: "review" | "autonomous";
  revision: string; enabled_until: string | null; available_submission_boards: string[];
  authorized_boards: string[]; gate_reasons: string[]; max_duration_hours: number };

export function WorkflowControl({ onChange }: { onChange: (policy: WorkflowPolicy | null) => void }) {
  const [policy, setPolicy] = useState<WorkflowPolicy | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const generation = useRef(0);
  const callback = useRef(onChange); callback.current = onChange;
  useEffect(() => {
    let active = true; let pending = false; const controller = new AbortController();
    async function poll() {
      if (pending || busyRef.current) return; pending = true; const revision = generation.current;
      try {
        const response = await fetch("/api/v1/workflow-policy", { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw Error("Workflow mode cannot be verified. Submission status is unknown.");
        const value: WorkflowPolicy = await response.json();
        if (active && !busyRef.current && revision === generation.current) { setPolicy(value); callback.current(value); setError(""); }
      } catch (e) { if (active && !busyRef.current && revision === generation.current) { setError(e instanceof Error ? e.message : "Mode unavailable"); callback.current(null); } }
      finally { pending = false; }
    }
    poll(); const timer = setInterval(poll, 5000);
    return () => { active = false; controller.abort(); clearInterval(timer); };
  }, []);
  async function change(mode: WorkflowPolicy["mode"]) {
    if (!policy || busyRef.current) return;
    busyRef.current = true; generation.current += 1; setBusy(true); setError("");
    try {
      const session = await fetch("/api/v1/session", { cache: "no-store" }).then(r => { if (!r.ok) throw Error("Dashboard unavailable"); return r.json(); });
      const response = await fetch("/api/v1/workflow-policy", { method: "POST", headers: {
        "Content-Type": "application/json", "X-JHB-CSRF": session.csrf_token }, body: JSON.stringify({ mode, revision: policy.revision }) });
      const value = await response.json();
      if (!response.ok) throw Error(value.detail || "Could not change workflow mode");
      setPolicy(value); callback.current(value);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not change workflow mode"); callback.current(null); }
    finally { busyRef.current = false; setBusy(false); }
  }
  const autonomous = policy?.mode === "autonomous" && !error;
  return <section className="panel workflow-panel" aria-label="Workflow mode">
    <div className="panel-heading"><div><h2>Workflow mode</h2><p>{error ? "Mode unverified" : autonomous ? "Independent review, then submission" : "You approve each application"}</p></div>
      <label className="workflow-toggle"><input type="checkbox" role="switch" aria-label="Full autonomy" checked={!!autonomous} disabled={busy || !policy || !!error} onChange={event => change(event.target.checked ? "autonomous" : "review")}/>Full autonomy</label></div>
    <p>{autonomous ? `Enabled until ${new Date(policy!.enabled_until!).toLocaleString("en-US", { timeZone: "America/Los_Angeles", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} PT. A separate reviewer checks every complete draft before submission.` : `Enable for ${policy?.max_duration_hours ?? 8} hours to let a separate reviewer authorize complete drafts. It returns to individual approval when the window expires.`}</p>
    <p className="privacy-caption">Only validated submission adapters: {policy?.available_submission_boards.join(", ") || "checking"}. Unknown facts, verification challenges and uncertain outcomes pause that application. Switching off prevents future autonomous submissions; an action already sent cannot be undone.</p>
    {policy?.gate_reasons.map(reason => <p className="form-error" key={reason}>{reason}</p>)}
    {policy?.requested_mode === "autonomous" && !autonomous && <button className="text-button" disabled={busy} onClick={() => change("review")}>Cancel autonomous authorization</button>}
    {error && <p role="alert" className="form-error">{error}</p>}
  </section>;
}

type Opening = { id: string; company: string; title: string; location: string; source: string; url: string | null;
  first_seen: number; date: string; classification_state: string; board: string; application_id: string | null; application_state: string | null; application_display_state?: string | null; filter_reasons: { category: string; evidence: string }[] };
type Page = { items: Opening[]; total: number; next_cursor: { before_time: number; before_id: string } | null };
const sourceLabel: Record<string, string> = { filtered: "Filtered out", discovered: "Discovered", queued: "Board check queued", running: "Checking destination", retry: "Board check retry", resolved: "Board identified", waiting_login: "Sign-in needed", waiting_captcha: "Verification needed", unknown: "Destination unresolved", failed: "Board check needs repair" };
const applicationLabel: Record<string, string> = { queued: "Queued for filling", running: "Preparing", waiting_review: "Draft ready", waiting_input: "Needs answer", submitted: "Submitted", skipped: "Filtered out", retry: "Fill retry", unsupported: "Adapter needed", failed: "Needs repair", waiting_login: "Sign-in needed", waiting_captcha: "Verification needed", submission_uncertain: "Outcome unverified", approval_queued: "Submission queued", submitting: "Submitting", needs_review: "Needs review", discarded: "Discarded" };

export function Openings() {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState("");
  const [older, setOlder] = useState<Opening[]>([]);
  const [cursor, setCursor] = useState<Page["next_cursor"]>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true; let pending = false; const controller = new AbortController();
    async function poll() {
      if (pending) return; pending = true;
      try {
        const response = await fetch("/api/v1/openings?limit=25", { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw Error("New opening status is unavailable");
        const value: Page = await response.json();
        if (active) { setPage(value); setError(""); }
      } catch (e) { if (active) setError(e instanceof Error ? e.message : "New opening status is unavailable"); }
      finally { pending = false; }
    }
    poll(); const timer = setInterval(poll, 5000);
    return () => { active = false; controller.abort(); clearInterval(timer); };
  }, []);
  async function more() {
    const next = older.length ? cursor : page?.next_cursor;
    if (!next || busy) return; setBusy(true);
    try {
      const response = await fetch(`/api/v1/openings?limit=25&before_time=${next.before_time}&before_id=${next.before_id}`, { cache: "no-store" });
      if (!response.ok) throw Error("Older openings could not be loaded");
      const value: Page = await response.json(); setOlder(rows => [...rows, ...value.items]); setCursor(value.next_cursor); setError("");
    } catch (e) { setError(e instanceof Error ? e.message : "Older openings could not be loaded"); }
    finally { setBusy(false); }
  }
  const items = [...new Map([...(page?.items || []), ...older].map(row => [row.id, row])).values()];
  const hasMore = older.length ? cursor : page?.next_cursor;
  return <section id="openings" className="panel applications-panel" style={{ marginTop: 24 }}>
    <div className="panel-heading"><div><h2>New openings from Phase 1</h2><p>Discovery, job-board resolution and application progress are tracked separately.</p></div><span className="muted-tag">{page?.total ?? "—"} discovered</span></div>
    {error && <p role="alert" className="form-error">{error}. Displayed data may be stale.</p>}
    <div className="table-scroll"><table><thead><tr><th>COMPANY & ROLE</th><th>DISCOVERED</th><th>DESTINATION CHECK</th><th>APPLICATION</th></tr></thead><tbody>{items.map(row => <tr key={row.id}>
      <td><div className="company-cell"><div><a href={row.url || undefined} target="_blank" rel="noreferrer">{row.company}</a><p>{row.title}</p><span>{row.location || row.source}</span></div></div></td>
      <td>{row.date}</td><td>{sourceLabel[row.classification_state] || row.classification_state}{row.board && row.board !== "unknown" && <span className="receipt-note">{row.board}</span>}{row.filter_reasons?.map((reason, i) => <span className="receipt-note" key={i}>{reason.evidence}</span>)}</td>
      <td>{row.application_id ? <a href={`#review/${row.application_id}`}>{applicationLabel[(row.application_display_state || row.application_state)!] || row.application_display_state || row.application_state}</a> : "Not started"}</td></tr>)}</tbody></table>
      {!items.length && <div className="table-empty">{page ? "No Phase 1 openings recorded yet." : "Loading Phase 1 openings…"}</div>}</div>
    <div className="table-footer">Showing {items.length} of {page?.total ?? "—"} openings{hasMore && items.length < 500 && <button className="text-button" disabled={busy} onClick={more}>{busy ? "Loading…" : "Load older openings"}</button>}<span>Newest 25 refresh every 5 seconds</span></div>
  </section>;
}
