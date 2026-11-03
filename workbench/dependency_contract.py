"""Dependency-only Planner contract and conservative, explicit human directives."""
import json
import re


def dependency_instruction(items, decisions):
    decisions=[{k:v for k,v in d.items() if k not in ('human_brief','human_summary','human_display_language')} for d in decisions]
    keys=[{'key':i.get('key') or i['id'],'id':i['id'],'acceptance':i.get('acceptance',[]),'acceptance_rules':i.get('acceptance_rules',{})} for i in items]
    return ('Update dependencies and the impact of answered human decisions. '
        'Return one JSON object. No change is valid: {"items":[],"actions":[],"summary":"No dependency changes required."}. '
        'items is an optional array (within configured Goal plan capacity) of updates to EXISTING items only. '
        'Each update identifies an existing key; optional id must identify that same item. '
        'Omit unchanged fields. depends_on is a unique string array of existing item IDs or keys, never self or cyclic. '
        'Human-requested acceptance-kind changes are allowed in this plan update: acceptance_rules is an object keyed by EXACT existing acceptance text. '
        'For example {"items":[{"key":"existing-key","acceptance_rules":{"existing criterion text":{"kind":"execution"}}}],"actions":[{"key":"existing-key","resolution":"review","instruction":"Review the existing successful check; no Builder rerun."}],"summary":"Human classification applied."}. '
        'Replace the entire rule with {"kind":"execution"}; do not carry document_paths/required_sections/format into an execution rule. '
        'Do not silently drop a requested classification change or substitute a Builder rework. Use review when existing evidence suffices. '
        'blocked_by_decision is a unique string array of decision IDs from this Goal, not item keys. '
        'priority is an INTEGER from 0 through 10000, not a numeric string. '
        'provisional_scope is exactly "none" or "local_files_and_tests". '
        'Every UNKNOWN dependency item must receive explicit depends_on and blocked_by_decision arrays; known unchanged items need not appear. '
        'Do not create, remove or rename items in this dependency refresh. '
        'State/instruction changes are INDEPENDENT: actions:[{"key":"existing-key","resolution":"rework","instruction":"Short scoped instructions from the human answer"}]. '
        'resolution is exactly rework, review, blocked, or superseded. Superseded needs an explicit human closure/cancel request, a reason, and optional superseded_by item ID/key; it is terminal, not accepted. A rework after an explicit human retry starts a new bounded four-chunk cycle; historical attempts/evidence remain. '
        'A review requires observed evidence. Example of only reopening an item with unchanged dependencies: '
        '{"items":[],"actions":[{"key":"existing-key","resolution":"rework","instruction":"Complete the existing module and run pytest."}],"summary":"Human retry applied."}. '
        'Never invent key/decision IDs. Unrelated work must not depend on a checkpoint. '
        'Copy full IDs/keys exactly from the catalog (never abbreviate or trim a final character). '
        'Only use actions for items linked to an ANSWERED decision; Human instructions/interrupts use human_actions in the same response, not actions. '
        'Existing item keys and IDs: '+json.dumps(keys)+'\nCurrent decisions: '+json.dumps(decisions,ensure_ascii=False))


def classification_requested(answer):
    return bool(re.search(r'acceptance_rules|reclassif|classif(?:y|ication).{0,60}(?:criterion|criteria|kind)|기준.{0,40}(?:분류|kind)|kind.{0,30}(?:execution|document)',answer,re.I|re.S))


def explicit_reopen(answer, item, allow_unscoped_retry=False):
    """Recognize narrow human no-structure-change directives, not arbitrary model prose."""
    stable=re.search(r'do not restructure|dependenc(?:y|ies) unchanged|no dependency edits|구조\s*변경\s*없|의존성.*(?:유지|변경하지)',answer,re.I)
    reopen=re.search(r'reopen|rework|다시\s*(?:열|연)|재개',answer,re.I)
    labels=[item['id'],item.get('key','')]
    match=re.match(r'(P\d+)\b',item.get('title',''))
    if match:labels.append(match[1])
    identified=any(label and re.search(r'(?<![A-Za-z0-9_])'+re.escape(label)+r'(?![A-Za-z0-9_])',answer,re.I) for label in labels)
    retry_lines=[s for s in answer.splitlines() if not re.search(r'\b(?:do not|don\x27t|never|defer|pause|stop)\b|금지|중지|보류',s,re.I)]
    retry=any(re.search(r'\bretry\b|reset.{0,40}(?:chunk|batch|limit)|approve.{0,40}rework|재시도|묶음.{0,20}초기화',s,re.I) and
        (allow_unscoped_retry or any(label and label in s for label in labels)) for s in retry_lines)
    return bool(stable and reopen and identified or retry)
