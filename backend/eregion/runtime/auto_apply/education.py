"""Source-backed education records and coverage of repeated form entries."""
import calendar
import hashlib
import json
import re

COMPONENTS = ("school","degree","field_of_study","start_date","end_date","graduation_status",
              "start_year","start_month","end_year","end_month")


def normalize_records(records):
    result=[];seen=set();ids=set()
    for record in records:
        value={k:str(record.get(k) or '').strip() for k in COMPONENTS[:6]}
        if not any(value.values()):continue
        fingerprint=json.dumps(value,sort_keys=True)
        if fingerprint in seen:continue
        seen.add(fingerprint)
        identifier=record.get('id') or 'edu-'+hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        if identifier in ids:identifier+='-'+str(len(result))
        ids.add(identifier);result.append({'id':identifier,**value})
    return result


def date_parts(text, *, confirmed_quote=""):
    text=str(text).strip().casefold()
    years=list(dict.fromkeys(re.findall(r'\b(?:19|20)\d{2}\b',text)))
    result={'year':years[0]} if len(years)==1 else {}
    named_months=[month for month in range(1,13) if re.search(
        r'\b(?:'+calendar.month_name[month].lower()+'|'+calendar.month_abbr[month].lower()+r')\b',text)]
    if len(named_months)==1:result['month']=str(named_months[0])
    if len(named_months)>1:return result
    # A graduation status describes the date; it is not part of its calendar
    # value. Project a local copy only, leaving the source fact untouched.
    calendar_text=re.sub(r'\s*\((?:expected|anticipated|projected|planned|completed|in progress)\)\s*$', '', text)
    iso=re.fullmatch(r'((?:19|20)\d{2})[-/](0?[1-9]|1[0-2])',calendar_text)
    reverse=re.fullmatch(r'(0?[1-9]|1[0-2])[-/]((?:19|20)\d{2})',calendar_text)
    if iso:result.update(year=iso[1],month=str(int(iso[2])))
    if reverse:result.update(year=reverse[2],month=str(int(reverse[1])))
    # Full ISO dates are unambiguous. Slash dates require either an
    # unambiguous day (>12) or a matching, explicitly written calendar date.
    full_iso = re.fullmatch(r'((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})', calendar_text)
    numeric = re.fullmatch(r'(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})', text)
    if re.match(r'^(?:19|20)\d{2}[-/]\d{1,2}',calendar_text) and not (iso or full_iso):
        result.pop('month',None)
        return result # Unrecognized alternatives must not turn into a different date.
    candidates = []
    if full_iso:candidates.append(tuple(map(int, full_iso.groups())))
    if numeric:
        first, second, year = map(int, numeric.groups())
        if second > 12 or first == second:candidates.append((year, first, second))
        elif first > 12:candidates.append((year, second, first))
        else:
            for month in range(1, 13):
                name = calendar.month_name[month] + '|' + calendar.month_abbr[month]
                for found in re.finditer(r'\b(?:'+name+r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+((?:19|20)\d{2})\b', confirmed_quote, re.I):
                    day, quoted_year = map(int, found.groups())
                    if quoted_year == year and (month,day) in {(first,second),(second,first)}:
                        candidates.append((year, month, day))
    valid = {(y,m,d) for y,m,d in candidates if 1 <= m <= 12 and 1 <= d <= calendar.monthrange(y,m)[1]}
    if len(valid) == 1:
        year,month,_ = valid.pop();result.update(year=str(year),month=str(month))
    return result


def date_format(field):
    hint=' '.join(str(field.get(k) or '') for k in ('label','placeholder','help')).upper()
    template=re.search(r'(?<![A-Z])(?:YYYY[-/]MM(?:[-/]DD)?|MM[-/]YYYY|DD[-/]MM[-/]YYYY|MM[-/]DD[-/]YYYY)(?![A-Z])',hint)
    if template:return template[0]
    # Workday splits dates into segment inputs named only by their ids (Adobe: "From* MM").
    segment=re.search(r'dateSection(Month|Day|Year)',str(field.get('key') or '')+' '+str(field.get('dom_id') or ''))
    if segment:return {'Month':'MM','Day':'DD','Year':'YYYY'}[segment[1]]
    native=field.get('type') or field.get('kind')
    return {'month':'YYYY-MM','date':'YYYY-MM-DD'}.get(native)


def format_date(field, value):
    template=date_format(field)
    if not template or not isinstance(value,str):return value
    parts=date_parts(value,confirmed_quote=value)
    # A single Workday segment needs only its own part ("Aug" -> MM, "2026" -> YYYY).
    if template=='YYYY':return parts['year'] if 'year' in parts else None
    if template=='MM':return f"{int(parts['month']):02d}" if 'month' in parts else None
    if 'year' not in parts or 'month' not in parts:return None
    year,month=int(parts['year']),int(parts['month'])
    day=None
    iso=re.search(r'(?<!\d)((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})(?!\d)',value)
    if iso:day=int(iso[3])
    numeric=re.fullmatch(r'(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})',value.strip())
    if numeric:
        first,second,_=map(int,numeric.groups())
        # The source, not the target's format, determines the calendar date.
        # date_parts already rejects ambiguous and invalid numeric calendars.
        if second>12 or first==second:day=second
        elif first>12:day=first
    for m in range(1,13):
        named=re.search(r'\b(?:'+calendar.month_name[m]+'|'+calendar.month_abbr[m]+r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+((?:19|20)\d{2})\b',value,re.I)
        if named:day=int(named[1]);month=m;year=int(named[2]);break
    if 'DD' in template and (day is None or not 1<=day<=calendar.monthrange(year,month)[1]):return None
    return template.replace('YYYY',str(year)).replace('MM',f'{month:02d}').replace('DD',f'{day:02d}' if day else '')


def combined_bindings(items):
    """Collapse redundant projections of the same date, never conflicting facts."""
    groups={}
    for item in items:groups.setdefault(item.get('field_key'),[]).append(item)
    result=[]
    for values in groups.values():
        parts={v.get('component') for v in values}
        prefix=next((p for p in ('start','end') if p+'_date' in parts and parts<={p+'_date',p+'_month',p+'_year'}),None)
        if prefix:result.append(next(v for v in values if v['component']==prefix+'_date'))
        else:result.extend(values)
    return result


def facts(profile, overrides=None):
    result={}
    for record in profile.get('education',[]):
        values={k:record.get(k,'') for k in COMPONENTS[:6]}
        confirmed = {component:item['value'] for component,item in (overrides or {}).get(record['id'], {}).items()
            if component in COMPONENTS and item.get('value') is not None}
        values.update({key:value for key,value in confirmed.items() if key in COMPONENTS[:6]})
        for part in ('start','end'):
            source = (overrides or {}).get(record['id'], {}).get(f'{part}_date', {}).get('source', {})
            values.update({f'{part}_{k}':v for k,v in date_parts(values[f'{part}_date'],
                confirmed_quote=source.get('quote','')).items()})
        values.update(confirmed)
        for component,value in values.items():
            if value:result[f"education:{record['id']}:{component}"]=value
    return result


def new_browser_bindings(state):
    """Retire DOM row identities; keep confirmed answers tied to their record.

    Workday reuses education-N IDs for different rows after a new session.
    Old field keys must never silently move an answer to a different school.
    """
    bindings = state.get('education_bindings', {})
    overrides = {key:dict(value) for key,value in state.get('education_answer_overrides', {}).items()}
    messages = {m['id']:i for i,m in enumerate(state.get('messages', [])) if m.get('id')}
    for key, binding in bindings.items():
        if key not in state.get('answers', {}):continue
        record = overrides.setdefault(binding['education_id'], {})
        source = state.get('answer_sources', {}).get(key, {})
        previous = record.get(binding['component'], {})
        if messages.get(previous.get('source', {}).get('message_id'), -1) > messages.get(source.get('message_id'), -1):continue
        record[binding['component']] = {'value':state['answers'][key], 'source':source}
    stale = set(bindings)
    stale.update(key for key in state.get('answers', {}) if re.search(r':education-\d+--', key))
    changes = {name:{key:value for key,value in state.get(name, {}).items() if key not in stale}
        for name in ('answers','answer_sources','field_decisions','option_paths')}
    return {**changes, 'education_bindings':{}, 'education_answer_overrides':overrides,
        'review_fields':[], 'review_digest':None}



def binding(field,state):
    saved=state.get('education_bindings',{}).get(field['key'])
    if not saved:return None
    # The raw DOM id changes on Workday re-renders; the stable key (with its entry order),
    # label and group identify the field.
    if {k:(saved.get('field') or {}).get(k) for k in ('key','label','group')}!={k:field.get(k) for k in ('key','label','group')}:return None
    return saved


def bound_value(field,state):
    saved=binding(field,state)
    return facts(state['profile'], state.get('education_answer_overrides')).get(saved['source_ref']) if saved else None


def repeatable(page):
    return any(c.get('kind')=='add_entry' and re.search(r'education|school|academic',
        c.get('label','')+' '+c.get('section','')+' '+str(c.get('automation_id') or ''),re.I) for c in (page or {}).get('controls',[]))


def gaps(state,fields,page,*,final=False):
    """Once a repeatable education section is offered, don't skip known records.

    Completion uses the bound fields' actual values, including optional fields.
    It does not turn an agent's 'done' statement into evidence of filling them.
    """
    from .autofill import desired_answer,matches
    records=state['profile'].get('education',[])
    if not records:return []
    current=any(binding(f,state) for f in fields)
    multiple=repeatable(page) or (state.get('education_required') and (current or final))
    if not current and not multiple:return []
    all_fields={f['key']:f for f in state.get('review_fields',[]) if final}
    all_fields.update({f['key']:f for f in fields})
    represented=set();pending=[]
    for field in all_fields.values():
        item=binding(field,state)
        if not item:continue
        value=desired_answer(field,state)
        if value is None:
            if item['component'] in {'start_date','end_date'} and date_format(field):pending.append('Date needs the precision required by the field: '+field['label'])
            continue
        if matches(field,value):
            if item['component']=='school':represented.add(item['education_id'])
        # Repeated rows share labels ("Field of Study" twice); the key says which one (replay).
        else:pending.append(f"{field['label']} ({field['key']})")
    if multiple:
        pending.extend(f"Add/complete education: {r.get('school') or r['id']} ({r.get('degree','')})"
            for r in records if r['id'] not in represented)
    # These columns commonly remain optional even when the resume supplies them.
    # The model assigns their record/component; they must not be silently skipped.
    for field in fields:
        if binding(field,state):continue
        label=field.get('label','').lower()
        # Veeva: "...average grade (GPA) for your last/current degree" asks for a GPA, and
        # "If you selected Other, please indicate your field of study" is a follow-up; neither
        # is a degree/field-of-study column, so requiring a mapping held review forever.
        if re.search(r'\b(gpa|grade|average)\b',label) or re.match(r'\s*if (?:you )?(?:selected|chose|picked)\b.*\bother\b',label):continue
        component=next((name for name,pattern in [('school',r'\b(school|university|college|institution)\b'),('degree',r'\bdegree\b'),('field_of_study',r'field of study|major|discipline')] if re.search(pattern,label)),None)
        if component and any(r.get(component) for r in records):pending.append('Map education field: '+field['label'])
        education_section=re.search(r'education|academic|school',field.get('section',''),re.I)
        if education_section and re.search(r'\b(from|to|start|end|graduation)\b',label+' '+field.get('group',''),re.I) and re.search(r'year|month|date',label,re.I):
            component='start' if re.search(r'\b(from|start)\b',label+' '+field.get('group',''),re.I) else 'end'
            part='month' if 'month' in label else 'year' if 'year' in label else 'date'
            if any((r.get(component+'_date') if part=='date' else date_parts(r.get(component+'_date','')).get(part)) for r in records):pending.append('Map education date: '+field['label'])
    return list(dict.fromkeys(pending))
