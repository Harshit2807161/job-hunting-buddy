"use client";

import { Openings, WorkflowControl, type WorkflowPolicy } from "./workflow-controls";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Application = { id: string; company: string; title: string; location: string; url: string | null; board: string;
  state: string; display_state?: string; approval_state?: string | null; pending_required_questions?: number; pending_candidate_questions?: number; pending_agent_tasks?: number; updated_at: number; date: string; attempts: number; filled_count: number; missing_count: number;
  has_screenshot: boolean; has_incident: boolean; inventory_ready: boolean; screenshot_at: number | null; confirmed_at: string | null; confirmed_date: string | null; sheet_synced: boolean };
type Question = { id: string; question: string; kind: string; updated_at: string; required: boolean; country_context: string | null;
  contexts: { job_hash: string; company: string; title: string; url: string | null; required: boolean; type: string; choices: string[]; reason: string; description?: string; description_truncated?: boolean; public_metadata_description?: string }[] };
type Overview = { generated_at: string; selected_date: string; timezone: string; storage_available: boolean; booklet_available: boolean;
  automation_paused: boolean; summary: { confirmed_today: number; confirmed_total: number; prepared_today: number; ready: number; legacy_review: number; running: number; queued: number; questions: number; uncertain: number; sheet_synced: number };
  daily: { date: string; confirmed: number; prepared: number }[]; states: Record<string, number>; source_states: Record<string, number>;
  applications: Application[]; applications_truncated: boolean; questions: Question[];
  activity: { job_hash: string; company: string; title: string; state: string; at: number }[];
  pipeline: { cycle_id: string; stage: string; status: string; updated_at: string; stale: boolean; reason_codes: string[] } | null;
  submission_pipeline: { status: string; stale: boolean; updated_at: string; active_jobs: string[] } | null };
type Tab = "all" | "submitted" | "waiting_review" | "attention";

const stateNames: Record<string, string> = { queued: "Queued", running: "Preparing", retry: "Retry scheduled", waiting_review: "Ready for review",
  waiting_input: "Needs your answer", waiting_login: "Sign-in needed", waiting_captcha: "Verification needed", submission_uncertain: "Outcome to verify",
  submitted: "Submitted", skipped: "Filtered out", unsupported: "Adapter needed", failed: "Technical review",
  approval_queued: "Submission queued", submitting: "Submitting", needs_review: "Needs review", discarded: "Discarded", history_hold: "Check previous application",
  agent_queued: "Agent queued", agent_working: "Agent working", needs_form_repair: "Needs form repair", needs_verification: "Needs verification" };
const approvalStages: Record<string, string> = { approved: "approval_queued", submitting: "submitting", needs_review: "needs_review", expired: "needs_review" };
const displayState = (a: { state: string; display_state?: string; approval_state?: string | null; pending_candidate_questions?: number; pending_agent_tasks?: number }) =>
  a.state === "waiting_input" && (!a.display_state || a.display_state === "waiting_input") && a.pending_candidate_questions === 0 ? (a.pending_agent_tasks ? "needs_form_repair" : "needs_verification") :
  a.display_state || (a.state === "waiting_review" ? (approvalStages[a.approval_state || ""] || a.state) : a.state);
const applicationLabel = (a: Application) => displayState(a) === "waiting_review" && !!a.pending_required_questions ? "Needs your answer" : displayState(a) === "waiting_review" && !a.inventory_ready ? "Inventory recheck" : stateNames[displayState(a)] || displayState(a);
const initials = (s: string) => s.trim().split(/\s+/).slice(0, 2).map(v => v[0]).join("").toUpperCase() || "?";
const today = () => new Intl.DateTimeFormat("en-CA", { timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const dateLabel = (day: string, short = false) => day ? new Date(day + "T12:00:00").toLocaleDateString("en-US", { month: short ? "short" : "long", day: "numeric", ...(short ? {} : { year: "numeric" }) }) : "—";
const timeLabel = (value: number | string) => new Date(typeof value === "number" ? value * 1000 : value).toLocaleTimeString("en-US", { timeZone: "America/Los_Angeles", hour: "numeric", minute: "2-digit" });

function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/></>,
    briefcase: <><rect x="3" y="7" width="18" height="14" rx="3"/><path d="M8 7V4h8v3M3 12h18M10 12v3h4v-3"/></>,
    chart: <><path d="M4 3v17h17M8 16v-5M13 16V7M18 16V4"/></>,
    message: <><path d="M21 11a8 8 0 0 1-8 8H7l-4 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/><path d="M7 8h10M7 12h6"/></>,
    arrow: <path d="m8 5 7 7-7 7"/>, check: <path d="m5 12 4 4L19 6"/>,
    link: <><path d="M14 3h7v7M21 3l-9 9M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/></>,
    search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
    close: <path d="m6 6 12 12M6 18 18 6"/>, image: <><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1"/><path d="m3 17 6-6 4 4 3-3 5 5"/></>,
    shield: <><path d="M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7z"/><path d="m8 12 3 3 5-6"/></>,
    spark: <><path d="m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z"/></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.grid}</svg>;
}

type AnswerResult = { resume_pending?: boolean; saved_at?: number; status: string; affected_jobs: string[]; resumed_jobs: string[]; automation_paused: boolean;
  applications: { job_hash: string; state: string; remaining_required_questions: number }[] };

function AnswerFeedback({ result, applications = [], paused, autonomous = false }: { result: AnswerResult; applications?: Application[]; paused?: boolean; autonomous?: boolean }) {
  return <div className="notice success" role="status"><b>Answer saved</b>
    {result.resume_pending && <p>Queue status could not be confirmed. Your answer is saved; refresh to check recovery before taking another action.</p>}
    <p>{(paused ?? result.automation_paused) ? "Filling is paused. Queued answers will be used when automation resumes." : "The agent will use your saved answer when filling resumes."} {autonomous ? "A separate reviewer checks the completed application before submission." : "Submission still requires your approval."}</p>
    {result.applications?.map(item => { const current = applications.find(a => a.id === item.job_hash); const state = current && current.updated_at >= (result.saved_at ?? Infinity) ? current.state : item.state;
      return <p key={item.job_hash}>{current ? `${current.company}: ` : "Application: "}{state === "waiting_input" ? (item.remaining_required_questions > 0 ? `Waiting for ${item.remaining_required_questions} more required answer${item.remaining_required_questions === 1 ? "" : "s"}.` : "Checking remaining form work.") : state === "queued" ? "Queued for filling." : stateNames[state] || state}</p>; })}
  </div>;
}

function QuestionForm({ question, onSaved, autonomous = false }: { question: Question; onSaved: (result: AnswerResult) => void; autonomous?: boolean }) {
  const [value, setValue] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(question.updated_at);
  const stale = revision !== question.updated_at;
  const context = question.contexts[0];
  const choices = question.kind === "role" ? ["sde", "ml"] : [...new Set(question.contexts.flatMap(c => c.choices))];
  const descriptions = [...new Map(question.contexts.filter(c => c.description).map(c => [c.description, c])).values()];
  const multi = ["multiselect", "checkboxes"].includes(context.type);
  const checkbox = context.type === "checkbox";
  const validAnswer = multi ? selected.length > 0 && selected.every(choice => choices.includes(choice))
    : checkbox ? ["true", "false"].includes(value)
    : choices.length ? choices.includes(value)
    : !!value.trim() && (context.type !== "number" || Number.isFinite(Number(value)));
  async function save(decline = false) {
    setBusy(true); setError("");
    try {
      const session = await fetch("/api/v1/session", { cache: "no-store" }).then(r => { if (!r.ok) throw Error("Local dashboard unavailable"); return r.json(); });
      const answer = multi ? selected : checkbox ? value === "true" : context.type === "number" ? Number(value) : value;
      const response = await fetch(`/api/v1/questions/${question.id}/answer`, { method: "POST", headers: {
        "Content-Type": "application/json", "X-JHB-CSRF": session.csrf_token }, body: JSON.stringify({ value: decline ? null : answer, decline, revision }) });
      if (!response.ok) { const message = await response.json(); throw Error(message.detail || "Could not save your answer"); }
      onSaved(await response.json());
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save your answer"); }
    finally { setBusy(false); }
  }
  return <article className="question-card">
    <div className="question-top"><span className="eyebrow">{context.company || "Application"}{question.contexts.length > 1 ? ` · ${question.contexts.length} applications` : ""}</span><span className={question.required ? "required-label" : "optional-label"}>{question.required ? "Required" : "Optional"}</span></div>
    <h3>{question.question}</h3><p className="question-role">{context.title}{question.country_context ? ` · ${question.country_context}` : ""}</p>
    {descriptions.map(c => <div className="question-description" key={c.description}><p>{descriptions.length > 1 && <b>{c.title}: </b>}{c.description}</p>{c.description_truncated && <p>Help text is clipped. Check the full instruction in the existing draft before answering.</p>}</div>)}
    {[...new Map(question.contexts.filter(c => c.public_metadata_description && c.public_metadata_description !== c.description).map(c => [c.public_metadata_description, c])).values()].map(c => <div className="question-description" key={`public-${c.public_metadata_description}`}><b>Public application guidance{question.contexts.length > 1 ? ` · ${c.title}` : ""}</b><p>{c.public_metadata_description}</p><p>Published by the employer. The agent will verify these details in your application before using your answer.</p></div>)}
    {context.reason && <p className="question-reason">Why your input is needed: {context.reason}</p>}
    {multi ? <div className="choice-grid">{choices.map(choice => <label key={choice}><input type="checkbox" checked={selected.includes(choice)} onChange={e => setSelected(e.target.checked ? [...selected, choice] : selected.filter(v => v !== choice))}/>{choice}</label>)}</div> :
      choices.length || checkbox ? <select aria-label={`Answer: ${question.question}`} value={value} onChange={e => setValue(e.target.value)}><option value="">Choose your answer</option>{(checkbox ? ["true", "false"] : choices).map(choice => <option key={choice} value={choice}>{checkbox ? choice === "true" ? "Yes — select checkbox" : "No — leave unchecked" : question.kind === "role" ? choice === "sde" ? "SDE resume" : "AI / ML resume" : choice}</option>)}</select> :
      <textarea aria-label={`Answer: ${question.question}`} value={value} onChange={e => setValue(e.target.value)} placeholder="Your exact answer" rows={3}/>
    }
    {stale && <div className="notice warning" role="alert">This question changed while you were answering. Your text is preserved. Review the latest question and choices, then <button className="text-button" onClick={() => { setRevision(question.updated_at); setSelected([]); if (choices.length && !choices.includes(value)) setValue(""); setError(""); }}>Use updated question</button>.</div>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <div className="question-actions"><button className="primary small" disabled={busy || stale || !validAnswer} onClick={() => save()}>{busy ? "Saving…" : "Save answer"}<Icon name="arrow" size={14}/></button>
      {!question.required && <button className="text-button" disabled={busy || stale} onClick={() => save(true)}>Leave unanswered</button>}
    </div><p className="privacy-caption">Saved for this employer. Filling can resume after required answers are complete; {autonomous ? "an independent reviewer checks the completed draft." : "submission always needs your approval."}</p>
  </article>;
}

type ReviewDetail = { location?: string; related_submissions?: { job_hash: string; url: string; company: string; title: string; location: string; confirmed_date: string | null }[];
  job_hash: string; state: string; display_state?: string; approval_state?: string | null; pending_candidate_questions?: number; pending_agent_tasks?: number; inventory_complete: boolean; resume_role: string | null;
  discard?: { state: string; tab_close: { state: string }; worker_stop: { state: string } };
  automation_paused: boolean; documents: { kind: string; filename: string }[]; reviewer_issues: string[];
  reviewer_verdict: string | null; reviewer_reviewed_at: string | null;
  approval_outcome?: { state: string; reason: string; approved_at: number; expires_at: number; expired: boolean; click_started: boolean | null } | null;
  role_fit_notes?: string[]; questions?: Question[]; agent_tasks?: {question: string; ref: string; task_kind: string; required: boolean}[];
  packet_revision?: string; draft_focus_available?: boolean; submission_supported?: boolean;
  screenshot?: { available: boolean; revision: string | null; captured_at: string | number | null };
  fields: { ref: string; question: string; type: string; required: boolean | null; status: string; category: string;
    answer: string | boolean | number | string[] | null; candidate_wording_required: boolean; proposed?: boolean; description?: string; description_truncated?: boolean }[];
  incident: { state: string; summary: string; blank_questions: { ref: string; question: string }[] } | null;
  approval: { can_approve: boolean; revision?: string; reason?: string; blank_questions: { ref: string; question: string; required: boolean; type: string }[];
    requires_role_fit_acknowledgment?: boolean; role_fit_warning?: { reason: string; gaps: string[] } | null;
    approval?: { state: string } } };

function discardMessage(result: ReviewDetail["discard"]) {
  const tab = result?.tab_close?.state;
  const closing = tab === "closed" || tab === "already_closed" ? "Its application tab is closed." :
    tab === "preserved" ? "The tab changed pages and was preserved." :
    tab === "no_captured_tab" ? "No owned application tab was available to close." :
    tab === "close_unconfirmed" ? "Tab closure needs verification; it will not be repeated blindly." :
    "Tab closure is waiting for the browser to be available.";
  const worker = result?.worker_stop?.state === "stopping" ? "Its worker is stopping safely." : "Its worker is stopped.";
  return `Application discarded. ${worker} ${closing} History is preserved.`;
}

function ReviewModal({ application: app, close, onChanged, autonomous = false }: { application: Application; close: () => void; onChanged: () => void; autonomous?: boolean }) {
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [error, setError] = useState("");
  const [acknowledged, setAcknowledged] = useState<string[]>([]);
  const [fitAcknowledged, setFitAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState("");
  const [answerFeedback, setAnswerFeedback] = useState<AnswerResult | null>(null);
  const [focusError, setFocusError] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionState, setActionState] = useState<string | null>(null);
  const busyRef = useRef(false);
  const lastActionRef = useRef<string | null>(null);
  const revisionRef = useRef<string | undefined>(undefined);
  const [detailRefresh, setDetailRefresh] = useState(0);
  useEffect(() => {
    let active = true; let inFlight = false; const controller = new AbortController();
    async function poll() {
      if (inFlight || busyRef.current) return; inFlight = true;
      try {
        const response = await fetch(`/api/v1/applications/${app.id}`, { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw Error("Detailed review is unavailable");
        const value: ReviewDetail = await response.json();
        if (active && !busyRef.current) {
          if (revisionRef.current !== value.approval.revision) { setAcknowledged([]); setFitAcknowledged(false); }
          revisionRef.current = value.approval.revision; setDetail(value); setActionState(null); setError("");
          if (lastActionRef.current === "approve" && value.state === "submitted")
            setSaved("Application submitted. Confirmation is recorded; spreadsheet status will update below.");
          if (lastActionRef.current === "discard" && value.discard) setSaved(discardMessage(value.discard));
        }
      } catch (e) { if (active) { setError(e instanceof Error ? e.message : "Detailed review is unavailable"); setDetail(null); setAcknowledged([]); setFitAcknowledged(false); } }
      finally { inFlight = false; }
    }
    poll(); const interval = setInterval(poll, 5000);
    return () => { active = false; controller.abort(); clearInterval(interval); };
  }, [app.id, detailRefresh]);
  const blanks = detail?.approval.blank_questions || [];
  const optionalBlanks = blanks.filter(q => !q.required);
  const currentApp: Application = detail ? { ...app, state: detail.state, display_state: detail.display_state,
    pending_candidate_questions: detail.pending_candidate_questions ?? detail.questions?.length ?? 0,
    pending_agent_tasks: detail.pending_agent_tasks ?? detail.agent_tasks?.length ?? 0,
    approval_state: detail.approval_state || detail.approval.approval?.state,
    filled_count: detail.fields.filter(f => f.status === "answered").length } : app;
  const stage = actionState || displayState(currentApp);
  const canApprove = !autonomous && detail?.submission_supported === true && detail?.state === "waiting_review" &&
    !!detail.approval.can_approve && !!detail.approval.revision && !["approval_queued", "submitting"].includes(stage) &&
    (!detail.approval.requires_role_fit_acknowledgment || fitAcknowledged);
  const canDiscard = !!detail && !["submitted", "submission_uncertain", "submitting", "discarded"].includes(stage);
  async function changeApproval(action: "approve" | "revoke") {
    lastActionRef.current = action;
    busyRef.current = true; setBusy(true); setActionError(""); setSaved("");
    if (action === "approve") setActionState("submitting");
    try {
      const session = await fetch("/api/v1/session", { cache: "no-store" }).then(r => r.json());
      const response = await fetch(`/api/v1/applications/${app.id}/${action}`, { method: "POST",
        headers: { "Content-Type": "application/json", "X-JHB-CSRF": session.csrf_token },
        body: JSON.stringify(action === "approve" ? { revision: detail?.approval.revision, acknowledged_blank_refs: acknowledged,
          acknowledge_role_fit_warning: fitAcknowledged } : {}) });
      const result = await response.json();
      if (!response.ok) throw Error(result.detail || "Submission request could not be completed");
      setActionState(action === "approve" ? (result.state === "approved" ? "approval_queued" : result.state) : null);
      setSaved(action === "revoke" ? "Approval revoked. This draft cannot be submitted." :
        result.state === "submitted" ? "Application submitted. Confirmation is recorded; spreadsheet status will update below." :
        result.state === "needs_review" || result.state === "waiting_review" ? (result.reason || "Submission paused for review. Your browser edits are preserved.") :
        result.state === "submitting" ? "Submitting the current browser form. Your manual edits are preserved." :
        result.state === "approved" ? "Submission queued for the current browser form. Your manual edits are preserved." :
        result.reason || "Submission request recorded. Check the live status below.");
      onChanged();
      const updated = await fetch(`/api/v1/applications/${app.id}`, { cache: "no-store" }).then(r => r.json());
      setDetail(updated); setAcknowledged([]); setFitAcknowledged(false);
    } catch (e) { setActionState(null); setActionError(e instanceof Error ? e.message : "Submission request could not be completed"); }
    finally { busyRef.current = false; setBusy(false); setDetailRefresh(v => v + 1); }
  }
  async function discardApplication() {
    lastActionRef.current = "discard";
    busyRef.current = true; setBusy(true); setActionError(""); setSaved("");
    try {
      const session = await fetch("/api/v1/session", { cache: "no-store" }).then(r => r.json());
      const response = await fetch(`/api/v1/applications/${app.id}/discard`, { method: "POST",
        headers: { "Content-Type": "application/json", "X-JHB-CSRF": session.csrf_token }, body: JSON.stringify({}) });
      const result = await response.json();
      if (!response.ok) throw Error(result.detail || "Application could not be discarded");
      setActionState("discarded");
      setSaved(discardMessage(result));
      onChanged(); setDetailRefresh(v => v + 1);
    } catch (e) { setActionError(e instanceof Error ? e.message : "Application could not be discarded"); }
    finally { busyRef.current = false; setBusy(false); }
  }
  async function focusDraft() {
    busyRef.current = true; setBusy(true); setFocusError(""); setSaved("");
    try {
      const session = await fetch("/api/v1/session", { cache: "no-store" }).then(r => r.json());
      const response = await fetch(`/api/v1/applications/${app.id}/focus`, { method: "POST",
        headers: { "Content-Type": "application/json", "X-JHB-CSRF": session.csrf_token },
        body: JSON.stringify({ revision: detail?.packet_revision }) });
      if (!response.ok) { const value = await response.json(); throw Error(value.detail || "Saved draft is unavailable. No new form was opened."); }
      setSaved("Your existing draft is focused in Chrome. Its submission guard remains enabled.");
    } catch (e) { setFocusError(e instanceof Error ? e.message : "Saved draft is unavailable. No new form was opened."); }
    finally { busyRef.current = false; setBusy(false); setDetailRefresh(v => v + 1); }
  }
  const displayAnswer = (answer: ReviewDetail["fields"][number]["answer"]) =>
    answer === null ? "Left blank" : typeof answer === "boolean" ? answer ? "Yes" : "No" : Array.isArray(answer) ? answer.join(", ") : String(answer);
  return <div className="modal-backdrop" onClick={close}><section className="review-modal" role="dialog" aria-modal="true" aria-labelledby="review-title" onClick={e => e.stopPropagation()}>
    <div className="review-heading"><div><div className="eyebrow">YOUR APPLICATION REVIEW</div><h2 id="review-title">{app.company}</h2><p>{app.title}</p><p>{detail?.location || app.location || "Location not recorded"}</p></div><button className="icon-button" aria-label="Close review" onClick={close}><Icon name="close"/></button></div>
    <div className="review-facts"><span className={`status ${stage}`} aria-live="polite"><i/>{actionState ? stateNames[actionState] || actionState : applicationLabel(currentApp)}</span><span>{currentApp.filled_count} recorded filled fields</span><button className="text-button" disabled={busy || !detail?.draft_focus_available || !detail.packet_revision} onClick={focusDraft}>Open saved draft<Icon name="link" size={13}/></button>{app.url && <a href={app.url} target="_blank" rel="noreferrer">Original posting<Icon name="link" size={13}/></a>}</div>
    <div className="review-body">
      {error && <div className="notice warning" role="alert">{error}</div>}
      {actionError && <div className="notice warning" role="alert">{actionError}</div>}
      {focusError && <div className="notice warning" role="alert">{focusError}</div>}
      {detail?.discard && <div className="notice" aria-live="polite">{discardMessage(detail.discard)}</div>}
      {!!detail?.related_submissions?.length && <div className="notice warning"><b>A similar role was already submitted</b><p>These are different posting IDs. They may be separate openings or a repost of the same role; this is not a confirmed duplicate. Review the previous application before approving.</p><ul>{detail.related_submissions.map(previous => <li key={previous.job_hash}><b>{previous.company} · {previous.title}</b><p>{previous.location || "Location not recorded"} · Recorded submission {previous.confirmed_date || "date unavailable"}</p><a href={`/#review/${previous.job_hash}`} target="_blank" rel="noreferrer">View confirmed application</a> · <a href={previous.url} target="_blank" rel="noreferrer">Previous posting</a></li>)}</ul></div>}
      {autonomous && <div className="notice"><b>Full autonomy is enabled</b><p>A separate reviewer must verify the complete draft, answers, documents and screenshot before submission. Required unknown facts still need your input.</p></div>}
      {detail?.incident && <div className="notice incident"><b>Submission quality incident</b><p>{detail.incident.summary}</p><ul>{detail.incident.blank_questions.map(q => <li key={q.ref}>{q.question}</li>)}</ul><p>The submission receipt remains recorded. This flag concerns unanswered questions.</p></div>}
      {detail && !detail.inventory_complete && <div className="notice warning">The saved field inventory is incomplete. Submission checks the current browser form and preserves your edits.</div>}
      {detail?.automation_paused && <div className="notice warning">Automation is paused. Saving an answer or approval does not restart the agent.</div>}
      <div className="document-strip"><Icon name="briefcase" size={17}/><div><b>{detail?.resume_role ? `${detail.resume_role.toUpperCase()} document variant` : "Document variant not recorded"}</b><p>{detail?.documents.map(d => `${d.kind.replaceAll("_", " ")}: ${d.filename}`).join(" · ") || "No document manifest available"}</p></div></div>
      {answerFeedback && <AnswerFeedback result={answerFeedback} paused={detail?.automation_paused} autonomous={autonomous}/>}
      {!!detail?.agent_tasks?.length && <div className="notice"><b>Agent work remaining</b><p>The last preparation left these items unfinished. Your current browser form is checked when you submit.</p><ul>{detail.agent_tasks.map(task => <li key={task.ref}>{task.question}: {task.task_kind === "document_generation" ? "prepare the application document" : task.task_kind === "narrative_generation" ? "draft and check a response" : "fill the saved booklet answer"}</li>)}</ul></div>}
      {!!detail?.questions?.length && <section><h3 className="review-section-title">Your input for this application</h3>{detail.questions.map(question => <QuestionForm key={question.id} question={question} autonomous={autonomous} onSaved={result => { setAnswerFeedback(result); setAcknowledged([]); setDetail(null); setDetailRefresh(v => v + 1); onChanged(); }}/>)}</section>}
      <h3 className="review-section-title">Every application question <span>{detail?.fields.length ?? "—"}</span></h3>
      {detail && detail.state !== "submitted" && <p className="review-current-note">These are the last saved answers. Edits you make in Chrome will be used when you submit the current browser form.</p>}
      {!detail && !error && <div className="table-empty">Loading the saved field inventory…</div>}
      <div className="review-field-list">{detail?.fields.map((field, i) => <div key={`${field.ref}-${i}`} className={field.answer === null ? "review-field blank" : "review-field"}><div className="review-field-title"><b>{field.question}</b><span>{field.required === null ? "Requirement unknown" : field.required ? "Required" : "Optional"}</span></div>{field.description && <div className="question-description"><p>{field.description}</p>{field.description_truncated && <p>Help text is clipped. Check the full instruction in the existing draft.</p>}</div>}{field.proposed && <div className="candidate-wording">Proposed wording · Check this grounded draft before approving.</div>}<p>{displayAnswer(field.answer)}</p>{field.candidate_wording_required && <div className="candidate-wording">The employer requests your own wording. The agent must not write this answer.</div>}</div>)}</div>
      {detail?.role_fit_notes?.length ? <div className="notice warning"><b>Role-fit considerations</b><ul>{detail.role_fit_notes.map((note, i) => <li key={i}>{note}</li>)}</ul><p>Review these potential gaps before approving. Factual application answers must remain accurate.</p></div> : null}
      {detail?.reviewer_issues.length ? <div className="notice warning"><b>Latest recorded reviewer / final-check notes</b><ul>{detail.reviewer_issues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>{detail.reviewer_reviewed_at && <p>Review recorded at {timeLabel(detail.reviewer_reviewed_at)} PT. Fresh checks still run before submission.</p>}</div> : null}
      {detail?.approval.approval && <div className="notice">Submission status: {stateNames[stage] || stage}. Your current browser answers are preserved.</div>}
      {detail?.approval_outcome && <div className="notice warning" role="status"><b>{detail.approval_outcome.click_started === false ? "Stopped before clicking Submit" : "Previous submission request needs attention"}</b><p>{detail.approval_outcome.reason}</p>{detail.approval_outcome.expired && <p>The previous approval expired. After the blocker is resolved, use Submit current browser form to approve your retained answers again.</p>}</div>}
      {detail?.approval.requires_role_fit_acknowledgment && detail.approval.role_fit_warning && <div className="notice warning"><b>Review this role's experience requirements</b><p>{detail.approval.role_fit_warning.reason}</p><ul>{detail.approval.role_fit_warning.gaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul><label><input type="checkbox" checked={fitAcknowledged} disabled={busy} onChange={e => setFitAcknowledged(e.target.checked)}/> I reviewed these experience gaps and still want to apply for this role.</label></div>}
      {!autonomous && optionalBlanks.length > 0 && <section className="blank-acknowledgments"><h3>Choose what stays blank</h3><p>For items still blank in Chrome, check each one you want to leave unanswered. Fields you filled in Chrome will use your current answer.</p>{optionalBlanks.map(q => <label key={q.ref}><input type="checkbox" checked={acknowledged.includes(q.ref)} onChange={e => setAcknowledged(e.target.checked ? [...acknowledged, q.ref] : acknowledged.filter(ref => ref !== q.ref))}/><span>Leave blank: {q.question}</span></label>)}</section>}
      {detail?.state === "waiting_review" && (detail.submission_supported === true ? autonomous ? <div className="notice"><p>The independent reviewer decides when this draft is ready. Turn off Full autonomy to use per-application approval.</p></div> :
        <div className="approval-actions"><button className="primary" disabled={busy || !canApprove} onClick={() => changeApproval("approve")}>{busy ? "Please wait…" : "Submit current browser form"}<Icon name="check" size={17}/></button><button className="text-button" disabled={busy} onClick={() => changeApproval("revoke")}>Revoke approval</button><p>Uses the answers and attachments currently in your Chrome tab. Your manual edits are preserved.</p>{!detail.approval.can_approve && detail.approval.reason && <p>{detail.approval.reason}</p>}</div> :
        <div className="notice warning"><b>{detail.submission_supported === false ? "Prepared for review. Automatic submission is not available for this board yet." : "Submission capability is unavailable. Refresh this review before approving."}</b><p>{detail.approval.reason || "You can inspect the saved answers and existing draft. No submission approval is available."}</p>{detail.approval.approval && <button className="text-button" disabled={busy} onClick={() => changeApproval("revoke")}>Revoke approval</button>}</div>)}
      {canDiscard && <div className="discard-actions"><button className="text-button danger-button" disabled={busy} onClick={discardApplication}>Discard application</button><p>Stops this application and closes its matching tab. Your history stays available.</p></div>}
      {saved && <div className={`notice ${["needs_review", "waiting_review", "submission_uncertain"].includes(stage) ? "warning" : "success"}`} role="status">{saved}</div>}
    </div>
    {(detail?.screenshot?.available ?? app.has_screenshot) ? <><p className="screenshot-caption">Saved at the preparation handoff{(detail?.screenshot?.captured_at ?? app.screenshot_at) ? ` · ${timeLabel((detail?.screenshot?.captured_at ?? app.screenshot_at)!)} PT` : ""}. Use Open saved draft to inspect its current state in the existing tab.</p><img className="review-image" src={`/api/v1/applications/${app.id}/screenshot${detail?.screenshot?.revision ? `?revision=${encodeURIComponent(detail.screenshot.revision)}` : ""}`} alt={`Saved review screenshot for ${app.company}`}/></> : <div className="screenshot-empty"><Icon name="image" size={32}/><h3>No validated screenshot saved</h3><p>Screenshot availability does not establish form completeness.</p></div>}
    {app.confirmed_at && <div className="receipt-banner"><Icon name="check" size={16}/>Submission confirmed {dateLabel(app.confirmed_date!, true)} at {timeLabel(app.confirmed_at)} PT{app.sheet_synced ? " · Sheets synced" : ""}.</div>}
  </section></div>;
}

export default function Dashboard() {
  const [data, setData] = useState<Overview | null>(null);
  // Static export can be opened days after build; first render must not depend
  // on the build machine's clock or differ from browser hydration.
  const [day, setDay] = useState("");
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("all");
  const [query, setQuery] = useState("");
  const [board, setBoard] = useState("all");
  const [preview, setPreview] = useState<Application | null>(null);
  const [view, setView] = useState("overview");
  const [allQuestions, setAllQuestions] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [answerFeedback, setAnswerFeedback] = useState<AnswerResult | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowPolicy | null>(null);
  const autonomous = workflow?.mode === "autonomous";
  useEffect(() => { setDay(today()); }, []);
  const reload = useCallback(() => setRefresh(v => v + 1), []);
  const openReview = useCallback((application: Application) => {
    window.history.replaceState(null, "", `#review/${application.id}`);
    setPreview(application);
  }, []);
  const closeReview = useCallback(() => {
    if (window.location.hash.startsWith("#review/")) window.history.replaceState(null, "", window.location.pathname + window.location.search);
    setPreview(null);
  }, []);
  useEffect(() => {
    const followReviewLink = () => {
      const match = /^#review\/([a-f0-9]{64})$/.exec(window.location.hash);
      const application = match && data?.applications.find(item => item.id === match[1]);
      if (application) setPreview(application);
    };
    followReviewLink();
    window.addEventListener("hashchange", followReviewLink);
    return () => window.removeEventListener("hashchange", followReviewLink);
  }, [data]);
  useEffect(() => {
    if (!day) return;
    let active = true; const controller = new AbortController(); let inFlight = false;
    async function poll() {
      if (inFlight) return; inFlight = true;
      try { const r = await fetch(`/api/v1/overview?day=${day}`, { cache: "no-store", signal: controller.signal });
        if (!r.ok) throw Error("The local API is unavailable");
        const value = await r.json(); if (active) { setData(value); setError(""); }
      } catch (e) { if (active) setError(e instanceof Error ? e.message : "Connection lost"); }
      finally { inFlight = false; }
    }
    poll(); const interval = setInterval(poll, 5000);
    return () => { active = false; controller.abort(); clearInterval(interval); };
  }, [day, refresh]);
  useEffect(() => { const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeReview(); }; window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey); }, [closeReview]);
  const apps = useMemo(() => (data?.applications || []).filter(a => {
    const text = `${a.company} ${a.title} ${a.location}`.toLowerCase();
    return text.includes(query.toLowerCase()) && (board === "all" || a.board === board) &&
      (tab === "all" || tab === "attention" ? tab !== "attention" || ["waiting_input", "waiting_login", "waiting_captcha", "submission_uncertain", "failed", "needs_review", "needs_form_repair", "needs_verification", "history_hold"].includes(displayState(a)) || displayState(a) === "waiting_review" && !a.inventory_ready : displayState(a) === tab && (tab !== "waiting_review" || a.inventory_ready));
  }), [data, query, board, tab]);
  const summary = data?.summary;
  const questionCount = data?.questions.length || 0;
  const chartMax = Math.max(1, ...(data?.daily.flatMap(d => [d.confirmed, d.prepared]) || [1]));
  const workerActive = !data?.automation_paused && data?.pipeline?.status === "running" && !data.pipeline.stale;
  const browserWaiting = !data?.automation_paused && !data?.pipeline?.stale && data?.pipeline?.reason_codes.includes("browser_capacity");
  const browserDisconnected = data?.pipeline?.reason_codes.includes("local_browser_disconnected");
  const activityTitle = error ? "Connection lost" : data?.automation_paused ? "Automation paused" : workerActive ? "Agent is working" : browserWaiting ? "Browser tabs need review" : browserDisconnected ? "Chrome disconnected" : data?.pipeline?.status === "blocked" ? "Agent paused" : summary?.running ? "Applications in progress" : "Workspace connected";

  return <div className="workspace">
    <aside className="sidebar"><div className="brand"><span className="brand-mark"><Icon name="spark" size={24}/></span><span>job hunting<br/><b>buddy</b></span></div>
      <div className="sidebar-label">WORKSPACE</div><nav>{[["overview", "grid", "Overview"], ["applications", "briefcase", "Applications"], ["openings", "search", "New openings"], ["questions", "message", "Your input"], ["activity", "chart", "Activity"]].map(([id, icon, label]) => <button key={id} className={view === id ? "nav-item active" : "nav-item"} onClick={() => { setView(id); document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }); }}><Icon name={icon}/>{label}{id === "questions" && questionCount > 0 ? <span className="nav-count">{questionCount}</span> : null}</button>)}</nav>
      <div className="sidebar-bottom"><div className="local-card"><Icon name="shield"/><div><b>Private by design</b><span>This workspace stays on your Mac.</span></div></div><div className="sidebar-footer"><span className="avatar">Y</span><div><b>Your workspace</b><span>Local · Pacific time</span></div><span className="tiny-dot"/></div></div>
    </aside>
    <main><header className="topbar"><span className="breadcrumb">Workspace <Icon name="arrow" size={12}/> <b>{view.charAt(0).toUpperCase()+view.slice(1)}</b></span><div className="topbar-right"><span className={error ? "connection offline" : "connection"}><i/>{error ? "Reconnecting" : "Live · updates every 5s"}</span><span className="top-avatar">Y</span></div></header>
      <div className="content"><section id="overview" className="page-heading"><div><div className="eyebrow">YOUR NEXT CHAPTER</div><h1>A little closer, every day<span>.</span></h1><p>Your applications, your agent, and everything that needs your attention.</p></div><label className="date-picker"><Icon name="clock" size={16}/><input type="date" value={day} onChange={e => e.target.value && setDay(e.target.value)} aria-label="Dashboard date"/></label></section>
      {error && <div className="notice warning" role="alert">{error}. Showing the last successful snapshot{data ? ` from ${timeLabel(data.generated_at)}` : ""}.</div>}
      {data && !data.storage_available && <div className="notice warning">Pipeline storage is unavailable. Counts cannot be verified.</div>}
      {data && !data.booklet_available && <div className="notice warning">The answer booklet is unavailable; pending-question status cannot be verified.</div>}
      {data?.automation_paused && <div className="notice warning">Application automation is paused. Changing workflow mode does not restart the preparation worker.</div>}
      {data?.applications.some(a => a.has_incident) && <div className="notice incident">A confirmed application has a submission quality incident. Open its review to inspect unanswered questions; receipt counts are preserved.</div>}
      <WorkflowControl onChange={setWorkflow}/>
      <section className="metrics" aria-label="Application metrics">{[
        { label: "Confirmed applications", value: summary?.confirmed_today, note: `${summary?.confirmed_total ?? "—"} total · receipt verified`, icon: "check", color: "mint" },
        { label: "Drafts prepared", value: summary?.prepared_today, note: `On ${dateLabel(day, true)} · before submission`, icon: "briefcase", color: "lavender" },
        { label: "Ready for review", value: summary?.ready, note: summary?.legacy_review ? `${summary.legacy_review} older drafts need inventory rechecks` : "Complete inventory · awaiting your review", icon: "image", color: "peach" },
        { label: "Needs your input", value: summary?.questions, note: "New questions · employer scoped", icon: "message", color: "blue" },
      ].map(metric => <div key={metric.label} className="metric-card"><div className="metric-top"><span>{metric.label}</span><span className={`metric-icon ${metric.color}`}><Icon name={metric.icon} size={17}/></span></div><div className="metric-value">{metric.value ?? "—"}</div><div className="metric-note">{metric.note}</div></div>)}</section>
      <div className="overview-grid"><section className="panel chart-panel"><div className="panel-heading"><div><h2>Momentum</h2><p>Small steps. Real progress.</p></div><span className="muted-tag">14 days · Pacific time</span></div><div className="chart-legend"><span><i className="confirmed-key"/>Confirmed</span><span><i className="prepared-key"/>Prepared</span></div>
        <div className="chart"><div className="chart-y"><span>{chartMax}</span><span>{Math.floor(chartMax/2)}</span><span>0</span></div><div className="chart-plot"><div className="grid-line top"/><div className="grid-line middle"/><div className="grid-line bottom"/>{data?.daily.map((d, i) => <div className="chart-column" key={d.date} title={`${dateLabel(d.date)}: ${d.confirmed} confirmed, ${d.prepared} prepared`}><div className="bar-pair"><span className="bar confirmed" style={{ height: `${d.confirmed/chartMax*100}%` }}/><span className="bar prepared" style={{ height: `${d.prepared/chartMax*100}%` }}/></div><span className="chart-x">{i % 3 === 0 || i === 13 ? dateLabel(d.date, true) : ""}</span></div>)}</div></div>
        <p className="chart-footnote">Confirmed counts come from submission receipts. Prepared counts use the saved draft date.</p>
      </section><section id="activity" className="panel agent-panel"><div className="panel-heading"><h2>Your agent</h2><span className={workerActive ? "live-pill" : "quiet-pill"}>{workerActive ? "WORKING" : "STATUS"}</span></div><div className="agent-status"><div className={workerActive ? "agent-orb active" : "agent-orb"}><Icon name="spark" size={26}/></div><h3>{activityTitle}</h3><p>{data?.pipeline ? `${data.pipeline.stage.replaceAll("_", " ")} · last heartbeat ${timeLabel(data.pipeline.updated_at)}` : "No pipeline heartbeat recorded yet."}</p></div>
        <div className="agent-counters"><div><b>{summary?.running ?? "—"}</b><span>preparing</span></div><div><b>{summary?.queued ?? "—"}</b><span>queued / retry</span></div><div><b>{summary?.sheet_synced ?? "—"}</b><span>sheet synced</span></div></div>
        {data?.pipeline?.stale && data.pipeline.status === "running" && <p className="form-error">Heartbeat is stale; worker activity is unconfirmed.</p>}
        {browserWaiting && <p className="agent-warning">New tabs are paused at the browser limit or while a new destination needs verification. Existing drafts stay open. Review completed applications below.</p>}
        {!data?.automation_paused && browserDisconnected && <p className="agent-warning">Reconnect your Chrome session to resume filling. Queued jobs keep their remaining attempts.</p>}
        {data?.submission_pipeline && <p className="submission-worker">Approval worker: {data.automation_paused ? "paused by the portal gate" : data.submission_pipeline.stale ? "heartbeat stale" : data.submission_pipeline.status.replaceAll("_", " ")}{!data.automation_paused && data.submission_pipeline.active_jobs.length ? ` · ${data.submission_pipeline.active_jobs.length} application in final checks` : ""}</p>}
        {summary?.uncertain ? <div className="agent-warning"><Icon name="shield" size={16}/>{summary.uncertain} outcome{summary.uncertain > 1 ? "s" : ""} need verification. No automatic replay.</div> : <div className="agent-safety"><Icon name="shield" size={15}/>Unknown answers pause their application.</div>}
      </section></div>
      <section id="questions" className="questions-section"><div className="section-heading"><div><h2>Your input{questionCount > 0 && <span className="count-badge">{questionCount}</span>}</h2><p>Answer once. The right applications pick up where they left off.</p></div></div>
        {answerFeedback && <AnswerFeedback result={answerFeedback} applications={data?.applications} paused={data?.automation_paused} autonomous={autonomous}/>}
        {questionCount ? <><div className="questions-grid">{data?.questions.slice(0, allQuestions || view === "questions" ? undefined : 4).map(q => <QuestionForm key={q.id} question={q} autonomous={autonomous} onSaved={result => { setAnswerFeedback(result); reload(); }}/>)}</div>{questionCount > 4 && view !== "questions" && <button className="text-button" onClick={() => setAllQuestions(v => !v)}>{allQuestions ? "Show fewer questions" : `Show all ${questionCount} pending questions`}</button>}</> : <div className="clear-state"><span><Icon name="check" size={18}/></span><div><b>{data?.booklet_available ? "You’re all caught up" : "Waiting for the answer booklet"}</b><p>{data?.booklet_available ? (autonomous ? "No new candidate questions right now. Complete drafts go to an independent reviewer." : "No new candidate questions right now. Each completed draft still requires your review and approval.") : "Question status will appear when private storage is available."}</p></div></div>}
      </section>
      <section id="applications" className="panel applications-panel"><div className="panel-heading"><div><h2>Applications</h2><p>A clear view of every stage.</p></div><span className="muted-tag">{data?.applications.length ?? "—"} tracked</span></div>
        <div className="table-toolbar"><div className="tabs">{[["all", "All"], ["submitted", "Submitted"], ["waiting_review", "Ready"], ["attention", "Needs attention"]].map(([id, label]) => <button key={id} className={tab === id ? "selected" : ""} onClick={() => setTab(id as Tab)}>{label}</button>)}</div><div className="table-filters"><label className="search"><Icon name="search" size={15}/><input aria-label="Search applications" placeholder="Search applications…" value={query} onChange={e => setQuery(e.target.value)}/></label><select value={board} onChange={e => setBoard(e.target.value)} aria-label="Filter job board"><option value="all">All boards</option>{[...new Set(data?.applications.map(a => a.board))].sort().map(b => <option key={b}>{b}</option>)}</select></div></div>
        <div className="table-scroll"><table><thead><tr><th>COMPANY & ROLE</th><th>BOARD</th><th>STATUS</th><th>LAST ACTIVITY</th><th>REVIEW</th></tr></thead><tbody>{apps.slice(0, 100).map(a => <tr key={a.id}><td><div className="company-cell"><div className="company-logo" style={{ background: `hsl(${parseInt(a.id.slice(0, 4), 16)%360} 35% 94%)` }}>{initials(a.company)}</div><div><a href={a.url || undefined} target="_blank" rel="noreferrer">{a.company}<Icon name="link" size={11}/></a><p>{a.title}</p><span>{a.location || "Location not recorded"}</span></div></div></td><td><span className="board-label">{a.board}</span></td><td><span className={`status ${displayState(a)}`}><i/>{applicationLabel(a)}</span>{a.confirmed_at && <span className="receipt-note"><Icon name="check" size={11}/>{a.sheet_synced ? "Receipt + sheet synced" : "Receipt verified"}</span>}</td><td><span className="activity-date">{dateLabel(a.date, true)}</span><span className="activity-time">{timeLabel(a.updated_at)} PT</span></td><td><button className="review-button" onClick={() => openReview(a)} aria-label={`Review ${a.company} application`}><Icon name="image" size={16}/>View</button></td></tr>)}</tbody></table>{!apps.length && <div className="table-empty">{data ? "No applications match these filters." : "Connecting to your local workspace…"}</div>}</div>
        <div className="table-footer">Showing {Math.min(apps.length, 100)} of {apps.length} matching applications{data?.applications_truncated ? " · API list limited to 5,000" : ""}<span>Live data · no simulated progress</span></div>
      </section><Openings/><footer className="page-footer"><span><Icon name="shield" size={13}/>Local workspace. Candidate data stays private.</span><span>{data ? `Last updated ${timeLabel(data.generated_at)} PT` : "Waiting for local API"}</span></footer>
      </div>
    </main>
    {preview && <ReviewModal key={preview.id} application={data?.applications.find(item => item.id === preview.id) || preview} close={closeReview} onChanged={reload} autonomous={autonomous}/>}
  </div>;
}
