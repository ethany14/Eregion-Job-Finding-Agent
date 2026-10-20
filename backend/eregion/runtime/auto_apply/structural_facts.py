"""Profile facts that a structurally identified control takes in a fixed format (no interpretation).

Workday's contact controls have stable automation ids. Its phone number control holds the national
number (the country code is a separate control), and its address city control holds the city alone.
When the profile states the fact in an unambiguous compound form, code takes the matching part:

  phoneNumber--phoneNumber   "+1 312-555-0142" -> "312-555-0142"   (an explicit +country prefix)
  address--city              "Ithaca, NY"      -> "Ithaca"         (exactly "City, XX")

Anything else (no explicit prefix, another city shape, another control) returns None and stays with
the model. A user's answer or the model's saved decision for the field always comes first.
"""
import re

_PHONE = re.compile(r'^\s*\+(\d{1,3})[\s.\-]+(\d[\d\s().\-]{5,})\s*$')
_CITY = re.compile(r'^\s*([^,]+?)\s*,\s*([A-Z]{2})\s*$')


def structural_fact(field, profile):
    identifier = str(field.get('dom_id') or '')
    if field.get('kind') not in {'text', 'tel'}:
        return None
    if identifier.endswith('phoneNumber--phoneNumber'):
        match = _PHONE.match(str(profile.get('phone') or ''))
        return match[2].strip() if match else None
    if identifier.endswith('address--city'):
        match = _CITY.match(str(profile.get('city') or ''))
        return match[1] if match else None
    return None
