"""Run-local model decisions: presentation/defaults are not candidate facts."""
import re
import unicodedata

from .contracts import candidate_answer
from .matching import is_consent, boolean_answer


def signature(field):
    return {k: field.get(k) for k in ("key", "label", "kind", "group")}


def source_value(state, reference):
    if reference == 'material:cover_letter':return state.get('cover_letter')
    if reference.startswith('education:'):
        from .education import facts
        return facts(state['profile'], state.get('education_answer_overrides')).get(reference)
    if reference.startswith('experience:'):
        from .experience import facts
        return facts(state['profile']).get(reference)
    kind, _, key = reference.partition(":")
    return state.get({"profile":"profile", "answer":"answers"}.get(kind, "__missing"), {}).get(key)


def active_decision(field, state):
    decision = state.get("field_decisions", {}).get(field["key"])
    if not decision or decision["field"] != signature(field): return None
    # Retire legacy fast guesses that treated a generic category as proof of
    # equivalence. Keep them in the audit log, but require a leaf observation.
    if decision.get('mode')=='equivalent' and (decision.get('origin')=='fast_options' or
            decision.get('reason')=='Fast semantic mapping from the confirmed source to an observed option.'):
        from .matching import field_category, resolve_option
        if field_category(field['label'])=='other' and not resolve_option(field['label'], decision['original_answer'],
                [{'label':decision['value'],'value':decision['value']}]):return None
    if any(ref.startswith('education:') for ref in decision['sources']):
        from .education import binding
        bound=binding(field,state)
        if not bound or bound['source_ref'] not in decision['sources']:return None
    if candidate_answer(field, state["profile"], state["answers"]) != decision["original_answer"]: return None
    if any(source_value(state, ref) != value for ref, value in decision["sources"].items()): return None
    if decision["mode"] == "default" and field["value"] and not field.get("invalid"):
        from .autofill import matches
        if not matches(field, decision["value"]): return None # Preserve a later manual choice.
    return decision


def unresolved_paths(fields, state):
    from .autofill import matches
    keys={f['key'] for f in fields};pending=[]
    for key,path in state.get('option_paths',{}).items():
        if key not in keys or source_value(state,path['source_ref']) != path['goal']: continue
        complete=False
        for field in fields:
            if field['key'] not in path['field_keys']: continue
            decision=active_decision(field,state)
            if matches(field,path['goal']) or (decision and decision['mode']=='equivalent' and
                decision['sources'].get(path['source_ref'])==path['goal'] and matches(field,decision['value'])):
                complete=True;break
        if not complete:pending.append(key)
    return pending


def protected(field):
    text = " ".join(str(field.get(k) or "") for k in ("label", "group", "help"))
    boolean_choice = any(boolean_answer(option.get("label", "")) is not None
                         for option in field.get("options", []))
    return boolean_choice or is_consent(field) or field["kind"] in {"checkbox", "password", "file"} or bool(re.search(
        r"\b(consent|agree|authorization|authori[sz]ed|sponsor\w*|visa|immigration|citizen\w*|eligib\w*|"
        r"salary|compensation|pay expectation|race|ethnic\w*|gender|disabil\w*|veteran|criminal|background check|"
        r"qualif\w*|degree|education|experience|certifi\w*|licen[cs]\w*|fluent|proficien\w*|relocat\w*|"
        r"enroll\w*|return to (?:the )?(?:program|school)|availability|start date|terms|privacy|marketing|subscribe)\b|授权|签证|担保|同意|薪资|性别|残疾|国籍",
        text, re.I))


def preserves_format(value, sources):
    """Check conservation, not site-specific labels or a phone-country table.

    The model decides which component belongs in which observed control. A phone
    can be reformatted, or partitioned into its 1–3 digit prefix and number.
    """
    def clean(text):
        return ''.join(c.casefold() for c in unicodedata.normalize('NFKD', str(text)) if c.isalnum())
    if len(sources) > 1 and clean(value) == clean("".join(str(v) for v in sources.values())):
        return True  # e.g. confirmed first + last name, or month + year.
    for ref, original in sources.items():
        if ref == "profile:phone" or (ref.startswith("answer:") and re.search(r"phone|telephone|mobile|tel:", ref, re.I)):
            digits = re.sub(r"\D", "", str(original))
            proposed = re.sub(r"\D", "", str(value))
            if not proposed: continue
            if digits == proposed: return True
            international = digits[2:] if str(original).strip().startswith('00') else digits
            if 1 <= len(proposed) <= 3 and international.startswith(proposed) and str(original).strip().startswith(('+', '00')):
                return True
            if len(proposed) >= 4 and international.endswith(proposed) and 0 < len(international)-len(proposed) <= 3:
                return True
        elif clean(value) == clean(original): return True
        # One delimited part of a compound fact, chosen by the model for a narrower control
        # (a City-only field takes "Ithaca" from "Ithaca, NY"). The part must be verbatim.
        elif clean(value) and any(clean(value) == clean(part) for part in re.split(r"[,;/|]", str(original))):
            return True
    return False
