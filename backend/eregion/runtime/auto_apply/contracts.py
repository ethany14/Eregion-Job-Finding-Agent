"""Application contracts: only explicit candidate facts become form answers."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

GREENHOUSE = {"boards.greenhouse.io", "job-boards.greenhouse.io",
              "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
LEVER = {"jobs.lever.co", "jobs.eu.lever.co"}
TERMINAL = {"submitted", "submission_unknown", "cancelled", "failed"}
PAUSED = {"needs_input", "awaiting_review"}
SUBMISSION_STATES = {"submitting", "submitted", "submission_unknown"}


def can_reopen(state):
    """Cancellation before submission is reversible; a submission attempt is not."""
    return (state["status"] in {"cancelled", "failed"}
        and not state.get("submission_started") and not state.get("confirmation")
        and not any(event.get("status") in SUBMISSION_STATES for event in state.get("events", [])))


def public_https_url(url: str) -> str:
    """Accept public career sites without accepting local or credential URLs."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("Use a public HTTPS job page without embedded credentials.")
    if not host or "." not in host or host.endswith((".localhost", ".local", ".internal")) or host == "metadata.google.internal":
        raise ValueError("Local and private network URLs are not job pages.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Local and private network URLs are not job pages.")
    for domain in (*GREENHOUSE, *LEVER, "myworkdayjobs.com"):
        if domain + "." in host:
            raise ValueError("This URL imitates a recruitment provider's domain.")
    host = host.encode("idna").decode("ascii")
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
        raise ValueError("Use a valid public job-site hostname.")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def posting(url: str) -> tuple[str, str, str]:
    """Return platform, deduplication identity, and an application URL."""
    parts = urlsplit(public_https_url(url))
    host = parts.hostname or ""
    path = parts.path.rstrip("/")
    if host in GREENHOUSE:
        match = re.fullmatch(r"/([\w-]+)/jobs/(\d+)(?:/apply)?", path)
        if not match:
            raise ValueError("Use the direct Greenhouse job URL ending in /jobs/JOB_ID.")
        region = "eu" if ".eu." in host else "global"
        return "greenhouse", f"greenhouse:{region}:{match[1]}:{match[2]}", urlunsplit(("https", host, path, "", ""))
    if host in LEVER:
        match = re.fullmatch(r"/([\w-]+)/([\w-]+)(?:/apply)?", path)
        if not match:
            raise ValueError("Use a direct Lever job URL.")
        return "lever", f"lever:{host}:{match[1]}:{match[2]}", f"https://{host}/{match[1]}/{match[2]}/apply"
    if host.endswith(".myworkdayjobs.com"):
        match = re.search(r"/(?:job|details)/(.+?)(?:/apply(?:/.*)?)?$", path)
        if not match:
            raise ValueError("Use a Workday job detail URL containing /job/ or /details/.")
        job_path = path.split("/apply", 1)[0]
        site = path[:match.start()].split('/')[-1]
        job = match[1].split('/')[-1].rsplit('_', 1)[-1]
        return "workday", f"workday:{host}:{site}:{job}", urlunsplit(("https", host, job_path, "", ""))
    platform = next((name for domain, name in (
        ("ashbyhq.com", "ashby"), ("smartrecruiters.com", "smartrecruiters"),
        ("icims.com", "icims"), ("taleo.net", "taleo"),
        ("successfactors.com", "successfactors"), ("jobvite.com", "jobvite"))
        if host == domain or host.endswith("." + domain)), "generic")
    if path in {"", "/"}:
        raise ValueError("Send a direct job page, not a site's home page.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in {"source", "ref", "referrer"}])
    canonical = urlunsplit(("https", host, path, query, ""))
    # The apply screen is a step of the posting, not another job. Keep the
    # requested URL for navigation; only the durable identity loses the step.
    identity_path = re.sub(r'/(?:apply|application)(?:/.*)?$', '', path, flags=re.I) or path
    identity_url = urlunsplit(("https", host, identity_path, query, ""))
    return platform, f"{platform}:" + hashlib.sha256(identity_url.encode()).hexdigest(), canonical


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Education(StrictModel):
    id: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_-]*$")
    school: str = Field(default="", max_length=300)
    degree: str = Field(default="", max_length=300)
    field_of_study: str = Field(default="", max_length=300)
    start_date: str = Field(default="", max_length=100)
    end_date: str = Field(default="", max_length=100)
    graduation_status: str = Field(default="", max_length=100)


class Experience(StrictModel):
    id: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_-]*$")
    title: str = Field(default="", max_length=300)
    company: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=300)
    start_date: str = Field(default="", max_length=300)
    end_date: str = Field(default="", max_length=300)
    current: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=2000)


class CandidateDetails(StrictModel):
    first_name: str = Field(default="", max_length=120)
    last_name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=80)
    linkedin: str = Field(default="", max_length=2048)
    github: str = Field(default="", max_length=2048)
    website: str = Field(default="", max_length=2048)
    school: str = Field(default="", max_length=300)
    degree: str = Field(default="", max_length=300)
    country: str = Field(default="", max_length=120)
    city: str = Field(default="", max_length=200)
    education: list[Education] = Field(default_factory=list, max_length=15)
    experience: list[Experience] = Field(default_factory=list, max_length=20)


class Candidate(CandidateDetails):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    phone: str = Field(default="", max_length=80)
    linkedin: str = Field(default="", max_length=2048)
    github: str = Field(default="", max_length=2048)
    website: str = Field(default="", max_length=2048)
    school: str = Field(default="", max_length=300)
    degree: str = Field(default="", max_length=200)
    country: str = Field(default="", max_length=120)
    city: str = Field(default="", max_length=200)


class StartApplication(StrictModel):
    url: str = Field(max_length=2048)
    profile: Candidate
    resume_base64: str = Field(min_length=1, max_length=14_000_000)
    resume_name: str = Field(default="resume.pdf", max_length=250)
    save_as_default: bool = True
    cover_letter: str | None = Field(default=None, max_length=20000)
    auto_submit: bool = False
    automation_mode: Literal["autofill", "hybrid"] = "hybrid"
    visual_fallback: bool = False
    reuse_answers: bool = True
    consent_policy: Literal["ask", "required_only"] = "ask"
    idempotency_key: str = Field(min_length=8, max_length=100)
    initial_message: str = Field(default="", max_length=4000)

    @field_validator("url")
    @classmethod
    def supported_url(cls, value):
        return posting(value)[2]

    @field_validator("resume_base64")
    @classmethod
    def valid_pdf(cls, value):
        try:
            content = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("Upload a valid PDF resume.") from exc
        if not content.startswith(b"%PDF-") or len(content) > 10 * 1024 * 1024:
            raise ValueError("Upload a PDF resume no larger than 10 MB.")
        return value


class ContinueApplication(StrictModel):
    expected_version: int = Field(ge=1)
    answers: dict[str, str | bool] = Field(default_factory=dict, max_length=100)
    confirm_submit: bool = False
    automation_mode: Literal["autofill", "hybrid"] | None = None
    visual_fallback: bool | None = None
    consent_policy: Literal["ask", "required_only"] | None = None
    # Review approval: which newly interpreted answers to reuse, and where.
    remember: dict[str, bool] = Field(default_factory=dict, max_length=200)
    memory_scopes: dict[str, Literal["global", "employer"]] = Field(default_factory=dict, max_length=200)

    @field_validator("answers")
    @classmethod
    def bounded_answers(cls, value):
        if any(len(key) > 1000 or (isinstance(answer, str) and len(answer) > 10_000)
               for key, answer in value.items()):
            raise ValueError("An answer is too long.")
        return value


class ReplyQuestion(StrictModel):
    request_id: str = Field(min_length=8, max_length=100)
    answers: dict[str, str | bool] = Field(min_length=1, max_length=100)

    @field_validator('answers')
    @classmethod
    def bounded_answers(cls, value):
        return ContinueApplication.bounded_answers(value)


class MemoryUpdate(StrictModel):
    value: str | bool | None = None
    scope: Literal["global", "employer"] | None = None

    @field_validator("value")
    @classmethod
    def bounded_value(cls, value):
        if isinstance(value, str) and len(value) > 10_000:
            raise ValueError("An answer is too long.")
        return value


class StandardAnswers(StrictModel):
    answers: dict[str, str] = Field(default_factory=dict, max_length=50)

    @field_validator("answers")
    @classmethod
    def bounded(cls, value):
        if any(len(answer) > 2000 for answer in value.values()):
            raise ValueError("An answer is too long.")
        return value


class VersionRequest(StrictModel):
    expected_version: int = Field(ge=1)


class AccountCredentials(StrictModel):
    # Password whitespace is significant, unlike ordinary candidate answers.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    expected_version: int = Field(ge=1)
    origin: str = Field(max_length=300)
    mode: Literal["login", "register"]
    username: str = Field(min_length=1, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1000)
    consents: dict[str, bool] = Field(default_factory=dict, max_length=20)


class AccountMode(StrictModel):
    expected_version: int = Field(ge=1)
    origin: str = Field(max_length=300)
    mode: Literal["login", "register"]


class ChatRequest(StrictModel):
    visual_fallback: bool | None = None
    text: str = Field(min_length=1, max_length=4000)
    field_key: str | None = Field(default=None, max_length=1000)
    message_id: str = Field(min_length=8, max_length=100)


class ResumeProfileRequest(StrictModel):
    resume_base64: str = Field(min_length=1, max_length=14_000_000)
    resume_name: str = Field(default="resume.pdf", max_length=250)
    valid_pdf = field_validator("resume_base64")(StartApplication.valid_pdf.__func__)


class SavedCandidateRequest(StrictModel):
    profile: CandidateDetails
    resume_base64: str | None = Field(default=None, max_length=14_000_000)
    resume_name: str = Field(default='resume.pdf', max_length=250)
    extracted: bool | None = None

    @field_validator('resume_base64')
    @classmethod
    def valid_resume(cls,value):
        return StartApplication.valid_pdf(value) if value is not None else None


class PreparationMessage(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    field: Literal["first_name", "last_name", "email", "phone", "linkedin", "github", "website", "school", "degree", "country", "city"] | None = None


def normalized_label(value):
    return re.sub(r"\s+", " ", value.replace("*", "").replace("✱", "").strip()).casefold().rstrip(":")


def candidate_answer(field, profile, answers):
    """Exact labels only. Unknown and legal/demographic answers are never guessed."""
    key = field["key"]
    if key in answers:
        return answers[key]
    label = normalized_label(field["label"])
    # Repeated education records must be bound individually. Applying the most
    # recent scalar school/degree to every row silently overwrites the bachelor.
    if profile.get("education") and re.search(r"school|university|college|degree|major|field of study",label):
        return None
    mapping = {"first name": "first_name", "last name": "last_name", "email": "email",
        "email address": "email", "phone": "phone", "phone number": "phone",
        "linkedin": "linkedin", "linkedin profile": "linkedin", "linkedin url": "linkedin",
        "github": "github", "github url": "github", "website": "website", "portfolio": "website",
        "school": "school", "school name": "school", "university": "school", "college/university": "school",
        "school / university": "school", "school or university": "school", "college": "school", "institution": "school",
        "degree": "degree", "country": "country", "country/region": "country", "country of residence": "country", "city": "city",
        "location (city)": "city", "current city": "city"}
    if label in mapping and mapping[label] in {"school", "degree", "country", "city"}:
        return profile.get(mapping[label]) or None
    if field["kind"] in {"checkbox", "radio", "select", "combobox"}:
        return None
    if label in {"full name", "name"}:
        return f"{profile['first_name']} {profile['last_name']}"
    return profile.get(mapping.get(label, "")) or None
