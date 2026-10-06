import asyncio
import hashlib
import json
import sqlite3
import time

import pytest

from jhb import eligibility
from jhb.applications import booklet, questions, queue, worker
from jhb.sources import simplify

EXCLUSIVE_EMPLOYMENT = ('At this time, we are only able to consider applicants who are '
                        'US Citizens or Green Card Holders for employment opportunities.')
REQUIRED_US_PERSON = ('U.S. Person Required: Must be a U.S. citizen, lawful permanent resident, '
                      'or protected individual such as an asylee or refugee in compliance with ITAR / EAR regulations.')


@pytest.mark.parametrize('text', [
    REQUIRED_US_PERSON,
    '<h3>Eligibility</h3><p>' + REQUIRED_US_PERSON + '</p>',
    'U.S. Person Required:\nMust be a US citizen, lawful permanent resident, or protected individual.',
    'Applicants must be a United States person.',
    'The person hired must therefore be either a US person as defined by ITAR.',
    'US person status is required for this position.',
    'Applicants are required to be a US person.',
    'Required qualifications:\nUS person status\nPython experience',
    'US persons only.',
])
def test_mandatory_us_person_is_closed_even_with_resident_and_protected_statuses(text):
    findings = eligibility.restrictions(text)
    assert {item['category'] for item in findings} == {'citizenship'}
    assert len(findings) == 1


@pytest.mark.parametrize('text', [
    'The person hired must therefore be either a US person as defined by ITAR or otherwise eligible for a federally issued export control license.',
    'US Person Required: US citizen or permanent resident, or eligible to obtain an export authorization.',
    'US Person Required:\nUS citizen or permanent resident or eligible for an export control license.',
    'US persons include US citizens, lawful permanent residents, refugees and asylees.',
    'Are you required to be a US person?',
    'Applicants must disclose US person status.',
    'US person status is preferred.',
    'US person status is not required.',
    'No US person status required.',
    'Applicants are not required to be a US person.',
    'We do not require applicants to be US persons.',
    'Preferred qualifications:\nUS person status',
    'Must support US person customers.',
])
def test_us_person_definitions_questions_nonrequirements_and_export_alternatives_remain_open(text):
    assert eligibility.restrictions(text) == []


@pytest.mark.parametrize('text', [
    EXCLUSIVE_EMPLOYMENT,
    'We only consider candidates who are U.S. citizens or lawful permanent residents.',
    'We can consider only US citizens or green-card holders for this role.',
    'We only hire United States citizens or permanent residents for employment.',
    'We only accept an applicant who is a US citizen or a green card holder.',
    'Must be US citizen or permanent resident.',
    'US citizens or lawful permanent residents only.',
    'Applicants must be U.S. citizens or green card holders.',
])
def test_exclusive_citizen_or_resident_employment_list_is_incompatible(text):
    assert {row['category'] for row in eligibility.restrictions(text)} == {'citizenship'}


@pytest.mark.parametrize('text', [
    'We do not only consider applicants who are US citizens or green card holders.',
    'Do you only consider applicants who are US citizens or green card holders?',
    'We only consider applicants who are US citizens or green card holders or eligible for an export control license.',
    'We only consider applicants who are US citizens or green card holders, refugees, or asylees.',
    'We welcome US citizens or green card holders for employment opportunities.',
])
def test_employment_status_rule_preserves_questions_and_open_alternatives(text):
    assert eligibility.restrictions(text) == []


@pytest.mark.parametrize("text", [
    "Applicants must be a U.S. citizen.", "U.S. citizenship is required.",
    "US citizens only.", "British citizenship required for this role.",
    "Must have an active security clearance.",
    "Candidates must be eligible to obtain and maintain a Secret clearance.",
    "Ability to obtain and maintain TS/SCI with a polygraph.",
    "Must be willing to undergo a polygraph examination.",
    "Security clearance: Secret", "Current TS/SCI clearance",
    "No security clearance required to apply, but must be eligible to obtain TS/SCI.",
    "Citizenship optional, security clearance required.",
    "<p>Minimum requirements:</p><ul><li>US citizenship required</li></ul>",
    "No clearance required and citizenship required.",
    "US citizenship is required; all applicants must disclose status.",
    "Must be a US citizen or permanent resident and must obtain TS/SCI.",
])
def test_required_conditions_are_excluded(text):
    assert eligibility.restrictions(text)


@pytest.mark.parametrize("title", [
    "Data Scientist (Secret Cleared), Tampa, Florida",
    "Top-Secret-Cleared Software Engineer", "Security Cleared Data Engineer",
    "Confidential-cleared Analyst",
])
def test_cleared_role_titles_stop_before_source_resolution(title):
    findings = eligibility.preliminary({"title": title})
    assert {item["category"] for item in findings} == {"security_clearance"}


@pytest.mark.parametrize("text", [
    "Required qualifications:\nSecret Cleared",
    "Must be Secret-cleared and complete medical clearance.",
    "Must support security-cleared clients while holding an active security clearance.",
])
def test_classified_cleared_requirements_in_description(text):
    assert {item["category"] for item in eligibility.restrictions(text)} == {"security_clearance"}


@pytest.mark.parametrize("text", [
    "Secret-cleared status is preferred.", "Secret-cleared status is not required.",
    "Must pass medical clearance.", "Drug clearance is required.",
    "Must support secret-cleared customers.", "Must work with security-cleared clients.",
])
def test_cleared_nonrequirements_remain_eligible(text):
    assert eligibility.restrictions(text) == []


@pytest.mark.parametrize("text", [
    "No security clearance is required.", "Security clearance is not required.",
    "Security clearance: None", "Citizenship optional.", "Citizenship disclosure required.",
    "Do you hold U.S. citizenship? Optional demographic survey.",
    "We hire without regard to citizenship, race, gender, or national origin.",
    "All applicants must be authorized to work in the United States.",
    "The successful applicant must pass a background check.",
    "No polygraph required.", "Applicants need not hold an active clearance.",
    "We do not require citizenship or clearance.", "Security clearance preferred.",
    "Medical clearance required for field work.",
    "Candidates will work with our security-cleared customers.",
    "All applicants must disclose citizenship status.",
    "No US citizenship required.",
    "No citizenship or security clearance is required.",
])
def test_nonrequirements_are_not_excluded(text):
    assert eligibility.restrictions(text) == []


@pytest.mark.parametrize('text', [
    'We cannot provide visa sponsorship for this position.',
    'Employment visa sponsorship is not available.',
    'Candidates must be able to work without sponsorship now or in the future.',
    'Applicants must not require immigration sponsorship.',
    'We do not sponsor employment visas.',
    'This position is not eligible for H-1B visa sponsorship.',
    'We do not currently sponsor visas.',
    'Sponsorship is not available for this role.',
    'We cannot provide sponsorship at this time or in the future.',
    'Visa sponsorship cannot be provided for this role.',
    'Visa Sponsorship:\nNot available',
    'This role does not offer sponsorship.',
    'We will not be able to provide visa sponsorship.',
    'We cannot consider candidates who require sponsorship.',
    'OPT is supported, but we cannot sponsor H-1B visas in the future.',
    'Security clearance not required and visa sponsorship is not available.',
    'This role does not qualify for employer-sponsored work authorization.',
    'Applicants must have work authorization that does not now or in the future require sponsorship of a visa for employment authorization in the United States.',
])
def test_explicit_sponsorship_denial_conflicts_with_saved_future_sponsorship_need(text):
    assert {item['category'] for item in eligibility.restrictions(text)} == {'visa_sponsorship'}


@pytest.mark.parametrize('text', [
    'Visa sponsorship is available for qualified candidates.',
    'Will you require visa sponsorship now or in the future?',
    'Are you eligible to work without visa sponsorship?',
    'No prior employment visa sponsorship experience required.',
    'We provide conference sponsorship to employees.',
    'No citizenship is required. Visa sponsorship is available.',
    'Experience with clearance systems and immigration document workflows preferred.',
    'There are no visa sponsorship restrictions for qualified candidates.',
    'Candidates without visa sponsorship are also welcome; sponsorship is available if needed.',
    'This role requires no visa sponsorship experience.',
    'We accept candidates with or without visa sponsorship.',
    'Visa sponsorship is not required to apply.',
    'No F-1 visa experience required.',
    'F-1 OPT and STEM OPT candidates are welcome.',
    'Sponsorship:\nAvailable',
    'Security clearance:\nNone',
    'CPT candidates are not eligible; OPT and STEM OPT candidates are welcome.',
])
def test_mentions_and_questions_are_not_a_sponsorship_denial(text):
    assert eligibility.restrictions(text) == []


def test_negated_citizenship_does_not_hide_separate_clearance_requirement():
    findings = eligibility.restrictions("No citizenship required and ability to obtain TS/SCI is required.")
    assert {item["category"] for item in findings} == {"security_clearance"}


@pytest.mark.parametrize('text', [
    'We are unable to consider candidates on F-1 OPT or STEM OPT.',
    'F-1 and OPT candidates are not eligible for this position.',
    'No OPT or CPT candidates.',
])
def test_explicit_student_visa_exclusions_are_not_generic_visa_mentions(text):
    assert {item['category'] for item in eligibility.restrictions(text)} == {'student_visa_restriction'}


def test_unrelated_optional_degree_does_not_mask_mandatory_clearance():
    findings = eligibility.restrictions('Must possess an active security clearance, a bachelor’s degree is preferred.')
    assert {item['category'] for item in findings} == {'security_clearance'}


@pytest.mark.parametrize("heading", ["Required:", "Minimum requirements:", "Basic Qualifications", "Qualifications"])
def test_bare_clearance_bullet_under_required_heading_is_excluded(heading):
    text = f"<h2>{heading}</h2><ul><li>TS/SCI with Polygraph</li><li>No prior experience required</li></ul>"
    findings = eligibility.restrictions(text)
    assert {item["category"] for item in findings} == {"security_clearance", "polygraph"}
    assert all(item["evidence"] == "TS/SCI with Polygraph" for item in findings)


@pytest.mark.parametrize("heading", ["Desired:", "Preferred Qualifications", "Nice-to-Have"])
def test_optional_clearance_section_is_not_a_mandatory_condition(heading):
    assert eligibility.restrictions(f"Required:\nPython experience\n{heading}\nCurrent TS/SCI clearance\nAbility to obtain security clearance") == []


def test_required_heading_does_not_turn_customer_description_into_condition():
    assert eligibility.restrictions("Required:\nExperience supporting security-cleared customers\nDesired:\nTS/SCI with Polygraph") == []


def test_itar_enumerated_residency_and_export_license_alternatives_are_preserved():
    text = ("To conform to US Government export regulations, applicant must be a (i) US citizen or national, "
            "(ii) US lawful, permanent resident (aka green card holder), (iii) Refugee under 8 U.S.C. § 1157, "
            "or (iv) Asylee under 8 U.S.C. § 1158, or be eligible to obtain the required authorizations "
            "from the US Department of State.")
    assert eligibility.restrictions(text) == []


@pytest.mark.parametrize("text", [
    "Applicant must be a US citizen or otherwise eligible for an export control license.",
    "Applicant must be a US citizen, lawful permanent resident, or refugee.",
])
def test_export_alternatives_do_not_become_citizenship_only_requirements(text):
    assert eligibility.restrictions(text) == []


def test_alternative_citizenship_does_not_mask_separate_mandatory_clearance():
    assert {x['category'] for x in eligibility.restrictions(
        "Applicants must be US citizens or permanent residents, security clearance required."
    )} == {"citizenship", "security_clearance"}


def test_required_clearance_bullet_stops_worker_before_browser(tmp_path, monkeypatch):
    monkeypatch.setattr("jhb.applications.cli_browser.BrowserUseCLI", lambda: pytest.fail("Excluded job opened browser"))
    result, _ = asyncio.run(worker.run_job(job("Required:\nTS/SCI with Polygraph\nDesired:\nCloud experience"),
                                          {"roles": {"sde": {}, "ml": {}}}, artifacts=tmp_path))
    assert result['state'] == 'skipped'


def job(description="Ordinary application development position."):
    item = {"dedupe_hash": "a"*64, "title": "Software Engineer", "company": "Synthetic",
            "url": "https://job-boards.greenhouse.io/example/jobs/123", "role_classes": "swe"}
    item["verified_job_description"] = {"status": "verified", "text": description,
        "source_url": eligibility.description_url(item), "retrieved_at": time.time(),
        "sha256": hashlib.sha256(description.encode()).hexdigest()}
    return item


@pytest.mark.parametrize("text", ["US citizenship required.", "Ability to obtain TS/SCI.", "Must pass a polygraph.", EXCLUSIVE_EMPLOYMENT, REQUIRED_US_PERSON])
def test_direct_live_worker_never_constructs_browser_for_excluded_jobs(tmp_path, monkeypatch, text):
    def forbidden(*args, **kwargs):
        pytest.fail("Excluded job must not access the candidate browser")
    monkeypatch.setattr("jhb.applications.cli_browser.BrowserUseCLI", forbidden)
    item = job(text)
    item['eligibility'] = {'state': 'eligible', 'policy': 'exclude-incompatible-employment-requirements-v2'}
    result, packet = asyncio.run(worker.run_job(item, {"roles": {"sde": {}, "ml": {}}}, artifacts=tmp_path))
    assert result["state"] == "skipped"
    assert result["filled"] == result["missing"] == []
    assert packet.exists()
    assert json.loads((packet.parent / "eligibility.json").read_text())["findings"]


@pytest.mark.parametrize('source', [False, True])
@pytest.mark.parametrize('description', [EXCLUSIVE_EMPLOYMENT, REQUIRED_US_PERSON])
def test_legacy_eligible_backlog_is_refiltered_before_either_queue_claim(source, description):
    from jhb.applications import source_queue
    module, table, state = (source_queue, 'application_sources', 'filtered') if source else (queue, 'applications', 'skipped')
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    module.enqueue(conn, [job()])
    stored = json.loads(conn.execute(f'SELECT job_json FROM {table}').fetchone()[0])
    stored['verified_job_description'] = job(description)['verified_job_description']
    stored['eligibility'] = {'state': 'eligible', 'policy': 'exclude-incompatible-employment-requirements-v2'}
    conn.execute(f'UPDATE {table} SET job_json=?', (json.dumps(stored),)); conn.commit()
    assert module.claim(conn) is None
    row = conn.execute(f'SELECT * FROM {table}').fetchone()
    assert row['state'] == state and row['attempts'] == 0
    outcome = json.loads(row['job_json'])['eligibility']
    assert outcome['policy'] == eligibility.POLICY_ID and outcome['state'] == 'skipped'
    assert outcome['findings'][0]['category'] == 'citizenship'
    conn.close()


def test_unavailable_description_handoffs_before_browser_without_candidate_question(tmp_path, monkeypatch):
    item = job()
    item.pop("verified_job_description")
    def unavailable(*args, **kwargs):
        raise RuntimeError("Do not print sensitive exception contents")
    monkeypatch.setattr(eligibility, "fetch_description", unavailable)
    monkeypatch.setattr("jhb.applications.cli_browser.BrowserUseCLI", lambda: pytest.fail("JD must be verified first"))
    result, packet = asyncio.run(worker.run_job(item, {}, artifacts=tmp_path))
    assert result["state"] == "waiting_input"
    assert result["missing"] == []
    assert "sensitive" not in packet.read_text()


def test_official_title_is_screened_even_when_feed_title_and_description_body_are_generic(tmp_path, monkeypatch):
    item = job()
    item['verified_job_description']['title'] = 'AI/ML Engineer 1 Top Secret/SCI w/Poly'
    monkeypatch.setattr('jhb.applications.cli_browser.BrowserUseCLI', lambda: pytest.fail('Official-title exclusion opened browser'))
    result, _ = asyncio.run(worker.run_job(item, {}, artifacts=tmp_path))
    assert result['state'] == 'skipped'
    assert result['eligibility']['description'] == item['verified_job_description']


def test_fetched_official_title_is_screened_before_preparation(monkeypatch):
    item = job()
    description = item.pop('verified_job_description')
    description['title'] = 'Software Engineer TS/SCI required'
    monkeypatch.setattr(eligibility, 'fetch_description', lambda _: description)
    assert eligibility.assess_job(item)['state'] == 'skipped'


def test_old_wrong_source_and_tampered_snapshots_cannot_bypass_fetch(monkeypatch):
    calls = []
    def fetch(item):
        calls.append(item)
        return job("US citizenship required.")["verified_job_description"]
    monkeypatch.setattr(eligibility, "fetch_description", fetch)
    for change in [{"retrieved_at": 0}, {"source_url": "https://example.test/fake"}, {"text": "Injected replacement"}]:
        item = job()
        item["verified_job_description"].update(change)
        assert eligibility.assess_job(item)["state"] == "skipped"
    assert len(calls) == 3


def test_queue_durably_skips_clearance_title_and_cannot_resume():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    item = job()
    item["title"] = "Software Engineer (TS/SCI with Polygraph)"
    assert queue.enqueue(conn, [item]) == 1
    row = conn.execute("SELECT * FROM applications").fetchone()
    assert row["state"] == "skipped"
    queue.resume(conn, row["job_hash"])
    assert queue.claim(conn) is None
    assert queue.enqueue(conn, [item]) == 0
    conn.close()


def test_claim_filters_legacy_queued_restriction_without_spending_attempt_or_losing_packet():
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    queue.enqueue(conn, [job()])
    row = conn.execute('SELECT * FROM applications').fetchone()
    value = json.loads(row['job_json'])
    value['raw'] = {'sponsorship': 'Does Not Offer Sponsorship'}
    conn.execute("UPDATE applications SET job_json=?,packet='private/existing-review.html'", (json.dumps(value),))
    conn.commit()
    assert queue.claim(conn) is None
    row = conn.execute('SELECT * FROM applications').fetchone()
    assert row['state'] == 'skipped' and row['attempts'] == 0
    assert row['packet'] == 'private/existing-review.html'
    assert json.loads(row['job_json'])['eligibility']['findings'][0]['category'] == 'visa_sponsorship'
    queue.resume(conn, row['job_hash'])
    assert queue.claim(conn) is None
    conn.close()


def test_preparation_queue_retains_structured_source_sponsorship_metadata():
    from jhb.store import Job
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    item = Job('synthetic', '1', 'Synthetic', 'Software Engineer', job()['url'],
               raw={'sponsorship': 'Does Not Offer Sponsorship'})
    assert queue.enqueue(conn, [item]) == 1
    assert queue.claim(conn) is None
    assert conn.execute('SELECT state FROM applications').fetchone()[0] == 'skipped'
    conn.close()


def test_answering_old_question_cannot_resume_a_skipped_application(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    item = job()
    queue.enqueue(conn, [item])
    item = queue.claim(conn)["job"]
    queue.finish(conn, item["dedupe_hash"], "skipped", "private/eligibility-review.html")
    path = tmp_path / "book.json"
    booklet.write_private(path, {"schema_version": 1, "answers": {}, "roles": {"sde": {}, "ml": {}}, "custom_answers": {}})
    question = questions.collect(item, {"state": "waiting_input", "missing": [
        {"question": "Synthetic employer question?", "ref": "question", "required": True}
    ]}, path)[0]
    questions.answer(question["id"], "Synthetic approved answer", path, connection=conn)
    assert conn.execute("SELECT state FROM applications").fetchone()[0] == "skipped"
    assert queue.claim(conn) is None
    conn.close()


def test_phase1_structured_citizenship_rule_and_title_description_exclusions():
    base = {"active": True, "is_visible": True, "company_name": "Synthetic Company",
            "id": "synthetic", "title": "Software Engineer", "locations": ["San Diego, CA"],
            "url": "https://job-boards.greenhouse.io/example/jobs/123"}
    for change in [{"sponsorship": "U.S. Citizenship is Required"},
                   {"title": "Software Engineer TS/SCI"},
                   {"description": "Ability to obtain and maintain a security clearance"}]:
        assert simplify.to_jobs([{**base, **change}]) == []
    assert len(simplify.to_jobs([{**base, "description": "No security clearance required; work authorization required."}])) == 1


def test_official_description_identity_and_redirect_host_are_bounded(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, maximum):
            assert maximum == 1_048_577
            return json.dumps({"id": 999, "content": "Unrelated role"}).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://boards-api.greenhouse.io/v1/boards/example/jobs/123"
            return Response()
    monkeypatch.setattr(eligibility.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="different job"):
        eligibility.fetch_description(job())
    request = eligibility.urllib.request.Request(eligibility.description_url(job()))
    with pytest.raises(ValueError, match="outside"):
        eligibility._OfficialRedirects().redirect_request(request, None, 302, "", {}, "https://example.test/redirect")


def test_html_only_description_is_unavailable_not_verified(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, maximum):
            return json.dumps({"id": 123, "content": "<p> </p><div></div>"}).encode()
    class Opener:
        def open(self, request, timeout): return Response()
    monkeypatch.setattr(eligibility.urllib.request, "build_opener", lambda *args: Opener())
    item = job()
    item.pop("verified_job_description")
    result = eligibility.assess_job(item)
    assert result["state"] == "waiting_input"
    assert result["verification"]["kind"] == "job_description"
    assert result["findings"] == []
