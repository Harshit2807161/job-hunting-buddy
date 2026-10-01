"""Filter correctness tests. These encode the judgment calls, so a future
regex tweak that breaks one of them is caught rather than silently changing
what lands in the inbox."""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
from jhb.matching import classify_title, is_us_location, listing_in_us


@pytest.mark.parametrize("title", [
    "Software Engineer, New Grad", "Associate Software Engineer", "Software Engineer I",
    "SDE I", "Member of Technical Staff", "Entry-level Software Developer",
    "Full Stack Cloud Software Engineer", "Backend Engineer", "Platform Engineer",
    "2027 Entry Level Software Engineer", "Web Developer", "Embedded Software Engineer",
])
def test_swe_matches(title):
    assert "swe" in classify_title(title).classes, title


@pytest.mark.parametrize("title", [
    "Machine Learning Engineer", "AI Engineer", "Applied AI Engineer",
    "Data Scientist", "Graduate Data Scientist", "Applied Scientist",
    "ML Research Scientist", "Associate AI/ML Engineer", "Research Scientist",
    "Computer Vision Engineer", "NLP Engineer", "Deep Learning Engineer",
])
def test_ml_matches(title):
    assert "ml" in classify_title(title).classes, title


@pytest.mark.parametrize("title,reason", [
    ("Senior Software Engineer", "senior"),
    ("Staff Software Engineer", "senior"),
    ("Principal Data Scientist", "senior"),
    ("Engineering Manager", "senior"),
    ("Software Engineer II", "leveled"),
    ("Member of Technical Staff 2", "leveled"),
    ("Software Engineering Intern", "intern"),
    ("Data Scientist Intern - Summer 2027", "intern"),
    ("Software Engineer Co-op", "intern"),
])
def test_excluded(title, reason):
    m = classify_title(title)
    assert m.excluded_by == reason, f"{title}: got {m.excluded_by}, want {reason}"


@pytest.mark.parametrize("title", [
    "Postdoctoral Scholar", "IT Officer", "Landscape Architecture Intern",
    "Data Engineer", "Data Analyst", "Business Analyst", "Mission Data Analyst",
    "Electrical Engineer", "Financial Analyst", "Product Manager",
])
def test_non_matches(title):
    assert not classify_title(title).any, title


@pytest.mark.parametrize("loc", [
    "San Jose, CA", "NYC", "SF", "LA", "Remote in USA", "Remote in US",
    "United States", "California", "Ohio", "FL", "South SF", "Austin, TX",
    "Washington, DC", "New York",
])
def test_us_locations(loc):
    assert is_us_location(loc) is True, loc


@pytest.mark.parametrize("loc", [
    "London, UK", "Toronto, ON, Canada", "India", "Paris, France",
    "Remote in Canada", "Germany", "Singapore", "Bangalore", "Europe",
    "Tel Aviv, Israel", "Sydney, Australia", "London",
])
def test_non_us_locations(loc):
    assert is_us_location(loc) is False, loc


def test_listing_in_us_any():
    assert listing_in_us(["London, UK", "Austin, TX"]) is True
    assert listing_in_us(["London, UK", "Toronto, ON, Canada"]) is False
    assert listing_in_us([]) is False


# --- regressions found by auditing live JobSpy drops (2026-09-14) ------------

@pytest.mark.parametrize("title", [
    "2027 Graduate - Developer - Cyber-Physical Systems",
    "Associate Software (Full-Stack) Developer",   # parenthetical breaks adjacency
    "Associate Developer - Information Technology",
    "Associate - Software Development & Engineering",
    "Junior Developer",
    "Entry-Level Developer",
])
def test_bare_developer_forms_match(title):
    """These were silently dropped as no-match before the parenthetical
    normalisation and bare-Developer patterns were added."""
    assert "swe" in classify_title(title).classes, title


@pytest.mark.parametrize("title", [
    "Developer Support Associate", "Business Developer", "Land Developer",
    "Real Estate Developer", "Business Development Manager",
])
def test_developer_false_positives_still_excluded(title):
    assert not classify_title(title).any, title


@pytest.mark.parametrize("loc", [
    "Austin, TX",              # LinkedIn form -- no trailing ", US"
    "Seattle, WA",
    "San Francisco, CA",
    "New York, United States",
    "Austin, TX, US",          # Indeed form
    "Hopkins, MN",
])
def test_jobspy_location_forms_are_us(loc):
    """JobSpy emits three different US location shapes depending on the site.
    Requiring a trailing ', US' dropped every LinkedIn row as non-US."""
    assert is_us_location(loc) is not False, loc
