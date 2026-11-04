"""Conservative header extraction shared by writers and material snapshots."""
import re

SECTIONS = {'summary', 'professional summary', 'profile', 'education', 'experience',
            'work experience', 'professional experience', 'selected projects', 'projects',
            'skills', 'technical skills', 'core competencies', 'certifications',
            'activities', 'volunteering', 'publications', 'awards'}
SKILL_SECTIONS = {'skills', 'technical skills', 'core competencies', 'technical expertise'}

def section_name(line):
    return re.sub(r'[:\s]+$', '', line.strip()).casefold()

def header_lines(text, preferred_name=None):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    boundary = next((i for i,line in enumerate(lines) if section_name(line) in SECTIONS), 0)
    lines = lines[:boundary]
    def noise(line):
        # Labels in a URL/email are contact data, not document disclaimers.
        if re.search(r'@|https?://|www\.|linkedin\.|github\.', line, re.I):
            return False
        return bool(re.fullmatch(r'(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?', line, re.I)
            or re.search(r'\b(?:confidential|fictional|synthetic|sample resume|curriculum vitae|do not distribute)\b', line, re.I)
            or section_name(line) in {'resume','cv'})
    def plausible(line):
        # A preferred name in parentheses is still part of the source name.
        # Only a single, letter-only name group is accepted here.
        name_line = re.sub(r"\(([A-Za-zÀ-ÖØ-öø-ÿ'’. -]+)\)", r'\1', line, count=1)
        words = name_line.split()
        return (1 <= len(words) <= 5 and len(line) <= 80 and not noise(line)
                and all(all(c.isalpha() or c in "-'’." for c in word) for word in words)
                and not re.search(r'\b(?:engineer|analyst|developer|manager|resume|curriculum|vitae)\b',line,re.I))
    candidates = [line for line in lines if plausible(line)]
    chosen = next((line for line in candidates if preferred_name and line.casefold()==preferred_name.casefold()), None)
    name = chosen or next(iter(candidates), None)
    if not name:
        return []  # Uncertain identity is omitted, never replaced by a document banner.
    contacts = [line for line in lines if line != name and not noise(line)
                and re.search(r'@|https?://|www\.|linkedin\.|github\.|[|,]|\+?\d[\d ()-]{6}',line)]
    return [name, *dict.fromkeys(contacts)]
