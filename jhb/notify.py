"""Email notification via Gmail SMTP.

Credentials come from a gitignored .env. The App Password is never logged --
errors are reported by SMTP error class, not by echoing the auth attempt.
"""

from __future__ import annotations

import hashlib
import html
import smtplib
import ssl
import time
from email.message import EmailMessage

from . import config

_CSS = """
body{margin:0;background:#f4f5f7;font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1a1d21}
.wrap{max-width:640px;margin:0 auto;padding:20px 16px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:#61666d;font-size:13px;margin:0 0 18px}
.card{background:#fff;border:1px solid #e3e5e8;border-radius:10px;padding:14px 16px;margin-bottom:10px}
.title{font-size:16px;font-weight:650;margin:0 0 3px}
.title a{color:#0b5cd5;text-decoration:none}
.co{color:#33383d;font-size:14px;margin:0 0 8px}
.meta{color:#61666d;font-size:12.5px;margin:0 0 10px}
.badge{display:inline-block;font-size:11px;font-weight:650;letter-spacing:.03em;
  padding:2px 7px;border-radius:20px;margin-right:5px;text-transform:uppercase}
.swe{background:#e5f0ff;color:#0b4ba8}
.ml{background:#ede6ff;color:#5324b8}
.src{background:#eceef0;color:#4a5057}
.btn{display:inline-block;background:#0b5cd5;color:#fff !important;text-decoration:none;
  font-size:13.5px;font-weight:600;padding:8px 15px;border-radius:7px}
.foot{color:#8b9097;font-size:12px;margin-top:18px;text-align:center}
.more{display:inline-block;font-size:11px;font-weight:650;padding:2px 7px;border-radius:20px;
  background:#fff4e0;color:#8a5200;margin-left:4px}
"""


def _card(j: dict) -> str:
    title = html.escape(j["title"])
    company = html.escape(j["company"] or "")
    url = html.escape(j["url"] or "", quote=True)
    try:
        locs = ", ".join(__import__("json").loads(j.get("locations") or "[]"))
    except Exception:
        locs = ""
    try:
        n_locs = len(__import__("json").loads(j.get("locations") or "[]"))
    except Exception:
        n_locs = 0
    locs = html.escape(locs)[:220]
    # Simplify lists one row per posting; those were folded into this card. Several
    # postings can share a location (21 Palo Alto reqs all say "California"), so
    # label by locations when they differ and by posting count when they don't.
    n_dupe = int(j.get("dupe_count") or 1)
    if n_dupe > 1:
        label = f"{n_locs} locations" if n_locs > 1 else f"{n_dupe} postings"
        extra = f'<span class="more">{label}</span>'
    else:
        extra = ""
    posted = ""
    if j.get("date_posted"):
        posted = time.strftime("%b %d", time.localtime(j["date_posted"]))
    badges = "".join(
        f'<span class="badge {c}">{c}</span>' for c in (j.get("role_classes") or "").split(",") if c
    )
    src = html.escape((j.get("source") or "").replace("jobspy:", ""))
    badges += f'<span class="badge src">{src}</span>' + extra
    meta = " &middot; ".join(x for x in (locs, posted) if x)
    return f"""<div class="card">
  <p class="title"><a href="{url}">{title}</a></p>
  <p class="co">{company}</p>
  <p class="meta">{badges}{'<br>' if meta else ''}{meta}</p>
  <a class="btn" href="{url}">Open posting &rarr;</a>
</div>"""


def render(jobs: list[dict], subject_prefix: str = "") -> tuple[str, str, str]:
    """Return (subject, html_body, text_body)."""
    n = len(jobs)
    swe = sum(1 for j in jobs if "swe" in (j.get("role_classes") or ""))
    ml = sum(1 for j in jobs if "ml" in (j.get("role_classes") or ""))
    if n == 1:
        subject = f"{subject_prefix}{jobs[0]['title']} — {jobs[0]['company']}"
    else:
        subject = f"{subject_prefix}{n} new new-grad roles ({swe} SWE · {ml} ML/AI)"

    cards = "\n".join(_card(j) for j in jobs)
    body = f"""<html><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
<div class="wrap">
  <h1>{n} new opening{'s' if n != 1 else ''}</h1>
  <p class="sub">{swe} SWE/SDE &middot; {ml} ML/AI/Data &middot; {time.strftime('%a %b %d, %I:%M %p')}</p>
  {cards}
  <p class="foot">job-hunting-buddy &middot; SimplifyJobs + JobSpy</p>
</div></body></html>"""

    lines = [f"{n} new opening(s) — {swe} SWE/SDE, {ml} ML/AI/Data", ""]
    for j in jobs:
        lines += [f"* {j['title']} — {j['company']}", f"  {j['url']}", ""]
    return subject, body, "\n".join(lines)


def send(jobs: list[dict], subject_prefix: str = "", dry_run: bool = False, details: str = "") -> bool:
    if not jobs:
        return False
    subject, body_html, body_text = render(jobs, subject_prefix)
    if details:
        body_text += "\n" + details
        body_html = body_html.replace("</body>", f"<pre>{html.escape(details)}</pre></body>")

    if dry_run:
        out = config.ROOT / "data" / f"preview-{int(time.time())}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body_html, encoding="utf-8")
        print(f"[dry-run] would email {len(jobs)} job(s): {subject}")
        print(f"[dry-run] preview written to {out}")
        return True

    if not config.SMTP_USER or not config.SMTP_PASS:
        raise RuntimeError(
            "SMTP credentials missing. Copy .env.example to .env and fill in "
            "JHB_SMTP_USER / JHB_SMTP_PASS (Gmail App Password)."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = config.EMAIL_TO
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    # Stable Message-ID derived from the exact set of jobs: if this batch is ever
    # delivered twice, Gmail collapses the copies instead of showing two emails.
    digest = hashlib.sha256(
        "|".join(sorted(j.get("dedupe_hash", j["url"]) for j in jobs)).encode()
    ).hexdigest()[:32]
    msg["Message-ID"] = f"<jhb-{digest}@job-hunting-buddy.local>"

    # Gmail occasionally drops the connection mid-handshake. Retry -- but ONLY
    # when the message was never handed over. If send_message() returned and the
    # failure came from QUIT/teardown, the mail is already delivered and a retry
    # would send a second copy.
    ctx = ssl.create_default_context()
    last: Exception | None = None
    for attempt in range(1, 4):
        delivered = False
        try:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                  context=ctx, timeout=45) as s:
                s.login(config.SMTP_USER, config.SMTP_PASS)
                s.send_message(msg)
                delivered = True          # server accepted it
            return True
        except smtplib.SMTPAuthenticationError:
            raise  # bad credentials never succeed on retry
        except (smtplib.SMTPException, OSError) as e:
            if delivered:
                # Failure was in teardown, after acceptance. Do not resend.
                return True
            last = e
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"SMTP send failed after 3 attempts: {type(last).__name__}: {last}")
