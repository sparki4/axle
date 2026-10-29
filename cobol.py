"""Structure extraction for COBOL sources (fixed or free format).

This does not execute or translate COBOL.  It reads a program and reports, mechanically and
without guessing intent: the data items, the files, every paragraph with what it performs,
calls, reads, writes and decides, and the numeric constants hard-coded in it.  Those
constants and decision points are where undocumented business rules usually live.
"""
import re

VERBS_NOT_PARAGRAPHS = {'EXIT', 'GOBACK', 'CONTINUE', 'END-IF', 'END-PERFORM', 'END-READ', 'END-EVALUATE', 'ELSE', 'STOP', 'DISPLAY'}
NAME = r'[A-Za-z0-9][A-Za-z0-9-]*'


def normalize(text):
    """Yield (line_number, code, comment) for each source line."""
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        fixed = len(line) > 7 and (line[:6].strip() == '' or line[:6].isdigit())
        if fixed:
            indicator = line[6]
            if indicator in '*/':
                yield number, '', line[7:].strip()
                continue
            code = line[7:72]
        else:
            code = line
        comment = ''
        if '*>' in code:
            code, _, comment = code.partition('*>')
        elif code.lstrip().startswith('*'):
            yield number, '', code.lstrip()[1:].strip()
            continue
        yield number, code.rstrip(), comment.strip()


def join_sentences(lines):
    """Group code lines into sentences ending with a period (not inside a number or literal)."""
    sentences, current, start = [], [], None
    for number, code, comment in lines:
        if not code.strip():
            continue
        if start is None: start = number
        current.append(code.strip())
        cleaned = re.sub(r"'[^']*'|\"[^\"]*\"", '', code)
        if re.search(r'\.\s*$', cleaned):
            sentences.append((start, ' '.join(current)))
            current, start = [], None
    if current:
        sentences.append((start, ' '.join(current)))
    return sentences


def scan(path, rel):
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = list(normalize(text))
    comments = {}
    pending = []
    for number, code, comment in lines:
        if comment and not code.strip():
            pending.append(comment)
        elif code.strip():
            comments[number] = pending
            pending = []
    program = dict(language='cobol', file=rel, program_id=None, data_items=[], conditions=[], files=[], copybooks=[],
                   paragraphs=[], calls=[], notes=[
                       'Extracted mechanically from the source. Nothing here is inferred intent: add `why` notes for the rules marked below.'])
    division, section = None, None
    paragraphs, current = [], None
    data_names = set()
    sentences = join_sentences(lines)
    for number, sentence in sentences:
        upper = sentence.upper().strip()
        m = re.match(rf'({NAME})\s+DIVISION\b', upper)
        if m:
            division = m[1]
            continue
        m = re.match(rf'PROGRAM-ID\.\s*({NAME})', upper)
        if m:
            program['program_id'] = m[1]
            continue
        m = re.match(rf'({NAME})\s+SECTION\.?$', upper)
        if m:
            section = m[1]
            if division == 'PROCEDURE':
                current = dict(name=m[1], kind='section', line=number, doc=comments.get(number, []), body=[])
                paragraphs.append(current)
            continue
        if division == 'ENVIRONMENT' or section == 'FILE-CONTROL' or upper.startswith('SELECT '):
            m = re.match(rf'SELECT\s+(?:OPTIONAL\s+)?({NAME})\s+ASSIGN\s+TO\s+(\S+)', upper)
            if m:
                program['files'].append(dict(name=m[1], assign=m[2].strip('.').strip("'\""), line=number))
            continue
        if upper.startswith('COPY '):
            m = re.match(rf'COPY\s+({NAME})', upper)
            if m: program['copybooks'].append(m[1])
            continue
        if division == 'DATA':
            m = re.match(rf'(\d{{1,2}})\s+({NAME})(.*?)\.?$', upper)
            if m:
                level, name, rest = int(m[1]), m[2], m[3]
                if level == 88:
                    program['conditions'].append(dict(name=name, line=number, rule=rest.strip()))
                    continue
                pic = re.search(r'PIC(?:TURE)?\s+(?:IS\s+)?(\S+)', rest)
                value = re.search(r'VALUE\s+(?:IS\s+)?(\'[^\']*\'|"[^"]*"|\S+)', rest)
                item = dict(level=level, name=name, line=number, pic=pic[1].rstrip('.') if pic else None,
                            value=value[1].rstrip('.') if value else None, occurs='OCCURS' in rest,
                            redefines='REDEFINES' in rest, comp=bool(re.search(r'COMP|BINARY', rest)))
                program['data_items'].append(item)
                data_names.add(name)
            continue
        if division == 'PROCEDURE':
            m = re.match(rf'^({NAME})\.\s*(.*)$', upper)
            if m and m[1] not in VERBS_NOT_PARAGRAPHS and not re.match(r'^\d', m[1]) and (m[2] == '' or True):
                head, tail = m[1], sentence[len(m[1]) + 1:].strip()
                if tail == '' or not re.match(r'^(MOVE|IF|PERFORM|ADD|DISPLAY|COMPUTE|READ|WRITE|OPEN|CLOSE|CALL|GO|STOP|EXIT|SUBTRACT|MULTIPLY|DIVIDE|EVALUATE|SET|INITIALIZE|ACCEPT|STRING|UNSTRING)\b', head):
                    current = dict(name=head, kind='paragraph', line=number, doc=comments.get(number, []), body=[])
                    paragraphs.append(current)
                    if tail:
                        current['body'].append((number, tail))
                    continue
            if current is None:
                current = dict(name='(start)', kind='paragraph', line=number, doc=[], body=[])
                paragraphs.append(current)
            current['body'].append((number, sentence))
    names = {p['name'] for p in paragraphs}
    for p in paragraphs:
        body = ' . '.join(s.rstrip('.') for _, s in p['body']).upper()
        quoted = re.sub(r"'[^']*'|\"[^\"]*\"", ' ', body)
        performs = sorted({t for t in re.findall(rf'PERFORM\s+({NAME})', quoted) if t in names})
        thru = re.findall(rf'THRU\s+({NAME})', quoted)
        calls = re.findall(r"CALL\s+['\"]([^'\"]+)['\"]", body)
        gotos = sorted({t for t in re.findall(rf'GO\s+TO\s+({NAME})', quoted) if t in names})
        mentioned = [t for t in re.findall(NAME, quoted) if t in data_names]
        written, read = set(), set()
        for m in re.finditer(rf'MOVE\s+(.*?)\s+TO\s+(.+?)(?=\s+(?:MOVE|IF|ADD|PERFORM|COMPUTE|DISPLAY|READ|WRITE|SUBTRACT|MULTIPLY|DIVIDE|CALL|GO|STOP|ELSE|END-|OPEN|CLOSE)|\s\.\s|$)', quoted):
            read.update(t for t in re.findall(NAME, m[1]) if t in data_names)
            written.update(t for t in re.findall(NAME, m[2]) if t in data_names)
        for m in re.finditer(rf'COMPUTE\s+({NAME})[^=]*=\s*(.+?)(?=\s+(?:MOVE|IF|ADD|PERFORM|COMPUTE|DISPLAY|READ|WRITE|SUBTRACT|CALL|GO|STOP|ELSE|END-)|$)', quoted):
            written.add(m[1]) if m[1] in data_names else None
            read.update(t for t in re.findall(NAME, m[2]) if t in data_names)
        for m in re.finditer(rf'(?:ADD|SUBTRACT)\s+(.+?)\s+(?:TO|FROM)\s+({NAME})', quoted):
            read.update(t for t in re.findall(NAME, m[1]) if t in data_names)
            if m[2] in data_names:
                written.add(m[2])
                read.add(m[2])
        for m in re.finditer(rf'GIVING\s+({NAME})', quoted):
            if m[1] in data_names: written.add(m[1])
        for m in re.finditer(rf'ACCEPT\s+({NAME})', quoted):
            if m[1] in data_names: written.add(m[1])
        read.update(set(mentioned) - written)
        files = []
        for verb in ('OPEN', 'READ', 'WRITE', 'REWRITE', 'CLOSE', 'DELETE'):
            for m in re.finditer(rf'\b{verb}\s+(?:(?:INPUT|OUTPUT|I-O|EXTEND)\s+)?({NAME})', quoted):
                files.append(f'{verb.lower()} {m[1]}')
        decisions = [re.sub(r'\s+', ' ', d).strip()[:120] for d in re.findall(r'\b(?:IF|WHEN|UNTIL)\s+(.+?)(?=\s+(?:THEN|MOVE|PERFORM|ADD|COMPUTE|DISPLAY|NEXT|CONTINUE|SET|GO|CALL|OPEN|CLOSE|STOP|READ|WRITE|ELSE|END-IF)\b|\s\.\s|$)', quoted)]
        constants = sorted({c for c in re.findall(r'(?<![A-Za-z0-9-])(\d+(?:\.\d+)?)(?![A-Za-z0-9-])', quoted) if c not in {'0', '1', '00', '01'}}, key=lambda c: (len(c), c))
        literals = re.findall(r"'([^']*)'|\"([^\"]*)\"", body)
        p.update(performs=performs, thru=thru, calls=calls, goto=gotos, reads=sorted(read), writes=sorted(written), files=files,
                 decisions=decisions, constants=constants, literals=[a or b for a, b in literals][:8],
                 statements=len([s for _, s in p['body']]), body=None)
        program['calls'] += calls
        p.pop('body')
    program['paragraphs'] = paragraphs
    program['calls'] = sorted(set(program['calls']))
    program['undocumented_paragraphs'] = [p['name'] for p in paragraphs if not p['doc']]
    program['business_rule_candidates'] = [dict(paragraph=p['name'], line=p['line'], constants=p['constants'], decisions=p['decisions'])
                                           for p in paragraphs if p['constants'] or p['decisions']]
    return program


def render(program):
    out = [f'# {program["program_id"] or program["file"]}  ({program["file"]})', '',
           'Mechanical extraction. Statements about *why* are left for people: fill in the TODO lines.', '']
    if program['files']:
        out.append('## Files')
        out += [f'- `{f["name"]}` assigned to `{f["assign"]}`' for f in program['files']]
        out.append('')
    if program['data_items']:
        out.append('## Data items')
        out += [f'- {"  " * max(0, d["level"] // 5)}{d["level"]:02d} `{d["name"]}`' + (f' PIC {d["pic"]}' if d['pic'] else '') +
                (f' VALUE {d["value"]}' if d['value'] else '') for d in program['data_items']]
        out.append('')
    out.append('## Paragraphs')
    for p in program['paragraphs']:
        out.append(f'### {p["name"]} (line {p["line"]})')
        out += [f'> {line}' for line in p['doc']]
        facts = []
        if p['performs']: facts.append('performs ' + ', '.join(p['performs']))
        if p['calls']: facts.append('calls program ' + ', '.join(p['calls']))
        if p['goto']: facts.append('jumps to ' + ', '.join(p['goto']))
        if p['files']: facts.append('file operations: ' + ', '.join(p['files']))
        if p['reads']: facts.append('reads ' + ', '.join(p['reads']))
        if p['writes']: facts.append('writes ' + ', '.join(p['writes']))
        if p['constants']: facts.append('hard-coded numbers: ' + ', '.join(p['constants']))
        if p['decisions']: facts.append('decides on: ' + '; '.join(p['decisions']))
        out += [f'- {f}' for f in facts] or ['- (no data or control flow found)']
        if not p['doc']: out.append('- TODO why: what business rule does this paragraph implement?')
        out.append('')
    return '\n'.join(out)
