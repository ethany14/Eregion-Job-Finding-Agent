"""Conservative, source-exact headers for common text/PDF resume layouts.

This extracts presentation structure, never candidate qualifications. Unknown
headers remain unowned text rather than inheriting a neighbouring employer.
"""
import re

MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?'
DATE = rf'(?:{MONTH}\s+\d{{4}}|\d{{4}}(?:-\d{{2}})?|Present|Current)(?:\s*\((?:expected|completed|in progress)\))?'
DATE_RANGE = re.compile(rf'(?P<start>{DATE})\s*(?:[–—]|\s-\s|\s+to\s+)\s*(?P<end>{DATE})', re.I)
DATE_ONLY = re.compile(DATE, re.I)
DEGREE = re.compile(r'\b(?:bachelor|master|doctor|certificate|diploma|[BM]\.?Eng\.?|[BM]\.?[ASE]\.?|Ph\.?D\.?)\b', re.I)
SCHOOL_WORD = r'(?:University|Institute|College|School|Academy|Polytechnic|Assembly)'
# A school either ends with its keyword (Stanford University) or starts with it
# (University of Illinois Urbana-Champaign, Institute of Technology ...).
SCHOOL = re.compile(rf'(?:The\s+)?(?:{SCHOOL_WORD}\b[^,]*|[A-Z][^,]*\b{SCHOOL_WORD}\b[^,]*)')
COMPANY_SUFFIX = re.compile(r'\b(?:Inc|LLC|Ltd|Corp(?:oration)?|Co|Company|Group|Labs?|Technologies|Systems|Health|Bank|GmbH|PLC)\b\.?', re.I)
TITLE_WORD = re.compile(r'\b(?:Engineer|Developer|Analyst|Scientist|Manager|Designer|Director|Lead|Intern|Consultant|Specialist|'
    r'Associate|Assistant|Fellow|Researcher|Coordinator|Nurse|Architect|Administrator|Officer|Representative|Technician|Teacher)\b', re.I)


def header_metadata(text, kind):
    """Return only substrings of an explicitly delimited header."""
    text = re.sub(r'\s+', ' ', text).strip()
    if re.match(r'^[-*•◦]\s', text):
        return None
    fields = [p.strip() for p in text.split('|')]
    dates = [p for p in fields if DATE_ONLY.fullmatch(p)]
    period = DATE_RANGE.search(text)
    if len(fields) >= 3 and dates and DATE_ONLY.fullmatch(fields[-1]):
        labels = [p for p in fields if not DATE_ONLY.fullmatch(p)]
        heading, organization = labels[0], labels[1] if len(labels) > 1 else None
        start, end = dates[0], dates[1] if len(dates) > 1 else None
        location = None
    elif period:
        start, end = period['start'], period['end']
        prefix = text[:period.start()].rstrip(' (|,–—-')
        location = None
        if '|' in prefix:
            labels = [p.strip() for p in prefix.split('|') if p.strip()]
            if not labels:return None
            heading, organization = labels[0], labels[1] if len(labels) > 1 else None
            location = labels[2] if len(labels) > 2 else None
        elif kind == 'experience' and re.search(r'\s[-–—]\s', prefix):
            heading, rest = re.split(r'\s[-–—]\s', prefix, maxsplit=1)
            # Inline layout: Role - Employer, City, State (dates).
            organization, _, location = rest.partition(', ')
            location = location or None
        elif kind == 'experience' and re.search(r'\s+at\s+', prefix):
            # Inline layout: Role at Employer, City (dates).
            heading, rest = re.split(r'\s+at\s+', prefix, maxsplit=1)
            organization, _, location = rest.partition(', ')
            location = location or None
        elif kind == 'education' and DEGREE.search(prefix):
            # Split at the school rather than a comma inside a degree title.
            split = next((m for m in re.finditer(r',\s+', prefix) if SCHOOL.match(prefix, m.end())), None)
            if not split:return None
            heading, organization = prefix[:split.start()], prefix[split.end():]
        elif kind == 'project' and prefix:
            heading, organization = prefix, None
        else:
            return None
    elif kind == 'education' and len(fields) == 2:
        # PDF extraction can join the degree and its single graduation date.
        # Require an explicit school/degree delimiter and keep Expected intact.
        graduation = re.search(rf'(?P<date>(?:Expected\s+)?{MONTH}\s+\d{{4}})\s*$', fields[1], re.I)
        if not graduation or not SCHOOL.fullmatch(fields[0]):return None
        heading = fields[1][:graduation.start()].strip().rstrip(' ,')
        if not DEGREE.search(heading):return None
        organization, location = fields[0], None
        start, end = None, graduation['date']
    else:
        return None
    if kind == 'education' and organization and not DEGREE.search(heading) and DEGREE.search(organization):
        heading, organization = organization, heading
    if not heading or (kind == 'experience' and not organization):return None
    return dict(heading=heading.strip(), organization=organization.strip() if organization else None,
                location=location.strip() if location else None, start_date=start, end_date=end)


def _plain_line(value):
    """A short label line: not a bullet, sentence, dated line or delimited header."""
    value = value.strip()
    return (bool(value) and len(value) <= 90 and not re.match(r'^[-*•◦]\s', value)
            and not re.search(r'[.!?]$', value) and not DATE_RANGE.search(value) and '|' not in value)


def two_line_metadata(first, second, kind):
    """Label line + dated line, in either order. Only explicit lines are used.

    'Acme Corp, Chicago' / 'Senior Engineer, 2021 - Present', or
    'Acme Corp<TAB>Mar 2021 - Present' / 'Senior Engineer'. Which line names the
    employer is decided from explicit company/school/degree/title words; when
    neither line is identifiable the header stays unowned.
    """
    def dated(value):
        period = DATE_RANGE.search(value)
        if not period:return None
        label = value[:period.start()].strip().rstrip(' (|,–—-\t').strip()
        rest = value[period.end():].strip(' )\t')
        if not label or rest and not re.fullmatch(r'[^A-Za-z]*', rest):return None
        return label, period['start'], period['end']
    for label_line, date_line in ((first, second), (second, first)):
        if not _plain_line(label_line):continue
        parsed = dated(date_line.strip())
        if not parsed:continue
        other, start, end = parsed
        label_line = label_line.strip()
        a, _, a_loc = label_line.partition(', ')
        b, _, b_loc = other.partition(', ')
        if kind == 'education':
            if DEGREE.search(b) and SCHOOL.fullmatch(a):heading, organization, location = b, a, a_loc
            elif DEGREE.search(a) and SCHOOL.fullmatch(b):heading, organization, location = a, b, b_loc
            else:continue
        elif kind == 'experience':
            a_company, b_company = bool(COMPANY_SUFFIX.search(a)), bool(COMPANY_SUFFIX.search(b))
            a_title, b_title = bool(TITLE_WORD.search(a)), bool(TITLE_WORD.search(b))
            if (a_company or b_title) and not (b_company or a_title):heading, organization, location = b, a, a_loc
            elif (b_company or a_title) and not (a_company or b_title):heading, organization, location = a, b, b_loc
            else:continue
        else:
            continue
        return dict(heading=heading.strip(), organization=organization.strip(),
                    location=location.strip() or None if location else None, start_date=start, end_date=end)
    return None


def _starts_header(value):
    """A wrapped tail such as '(expected)) - In progress' never starts an entry."""
    value = value.strip()
    return bool(value) and not re.match(r'^[-*•◦]\s', value) and not re.match(r'^[()\[\],;:]', value) and not value[0].islower()


def entry_spans(text, kind):
    """Recognize single/wrapped headers; coordinates remain in original text."""
    lines = list(re.finditer(r'(?m)^.+$', text))
    headers = []
    consumed = -1
    def gap(a, b):
        return text[lines[a].end():lines[b].start()].count('\n') > 1
    for i, line in enumerate(lines):
        if i <= consumed or not _starts_header(line.group()):continue
        metadata = None
        # PDF wrapping can put the date on the next line. Stop before bullets
        # and blank paragraphs so prose never becomes another entry's header.
        for j in range(i, min(i + 3, len(lines))):
            if j>i and (re.search(r'[.!?]\s*$',line.group()) or not
                ('|' in line.group() or re.search(r'\s[-–—]\s',line.group()) or DEGREE.search(line.group()))):break
            if j > i and (gap(j-1, j) or re.match(r'^\s*[-*•◦]\s', lines[j].group())):break
            candidate = text[line.start():lines[j].end()]
            metadata = header_metadata(candidate, kind)
            if metadata:
                # A date range can wrap inside its parentheses. Keep the tail
                # (for example '(expected)) - In progress') with THIS entry.
                while (j + 1 < len(lines) and candidate.count('(') > candidate.count(')')
                       and not gap(j, j+1) and not re.match(r'^\s*[-*•◦]\s', lines[j+1].group())):
                    j += 1
                    candidate = text[line.start():lines[j].end()]
                    metadata = header_metadata(candidate, kind) or metadata
                headers.append((line.start(), metadata));consumed = j;break
        if metadata or i <= consumed or i + 1 >= len(lines) or gap(i, i+1):continue
        metadata = two_line_metadata(line.group(), lines[i+1].group(), kind)
        if metadata:
            headers.append((line.start(), metadata));consumed = i + 1
    if not headers:return [(0, len(text), {})]
    result = []
    # Keep an unrecognized preamble independent of the first known owner.
    if text[:headers[0][0]].strip():result.append((0, headers[0][0], {}))
    for index, (start, metadata) in enumerate(headers):
        end = headers[index+1][0] if index+1 < len(headers) else len(text)
        result.append((start, end, metadata))
    return result
