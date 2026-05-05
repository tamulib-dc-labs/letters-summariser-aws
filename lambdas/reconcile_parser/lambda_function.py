import boto3
import json
import re

s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def force_string(value):
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return str(value).strip() or None


def parse_s3_uri(uri):
    uri = force_string(uri)
    if not uri:
        raise ValueError("S3 URI is empty or None")
    if not uri.startswith('s3://'):
        raise ValueError(f"Invalid S3 URI: {uri}")
    parts = uri[5:].split('/', 1)
    if len(parts) != 2:
        raise ValueError(f"Could not parse bucket/key from URI: {uri}")
    return parts[0], parts[1]


def safe_id(value):
    return re.sub(r'[^a-zA-Z0-9\-]', '_', str(value))


# ─── Extract thinking block + tool result ─────────────────────────────────────

def extract_thinking_and_tool_result(raw, expected_tool_name=None):
    if 'Body' in raw and isinstance(raw['Body'], dict) and 'content' in raw['Body']:
        print("[DEBUG] Unwrapping Step Functions 'Body' envelope")
        raw = raw['Body']

    stop_reason = raw.get('stop_reason', 'unknown')
    content     = raw.get('content', [])

    if not content:
        raise ValueError(f"No 'content' in Bedrock response. stop_reason={stop_reason}")

    block_types = [
        b.get('type', '<missing>') if isinstance(b, dict) else f"<{type(b).__name__}>"
        for b in content
    ]
    print(f"[DEBUG] stop_reason={stop_reason}, block_types={block_types}")

    thinking_block = None
    tool_use_block = None

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'thinking':
            thinking_block = block
        elif block.get('type') == 'tool_use':
            if expected_tool_name and block.get('name') != expected_tool_name:
                print(f"[WARN] Expected tool '{expected_tool_name}', got '{block.get('name')}' — using anyway")
            tool_use_block = block

    if tool_use_block:
        tool_input = tool_use_block.get('input')
        if isinstance(tool_input, str):
            tool_input = json.loads(tool_input)
        if not isinstance(tool_input, dict):
            raise ValueError(
                f"tool_use block has no 'input' dict. "
                f"tool={tool_use_block.get('name')}, keys={list(tool_use_block.keys())}"
            )
        print(
            f"[OK] tool={tool_use_block.get('name')}, "
            f"thinking={'yes (signature preserved)' if thinking_block else 'no'}"
        )
        return thinking_block, tool_use_block, tool_input

    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text', '').strip()
            if text:
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    try:
                        print(f"[WARN] tool_use not invoked (stop_reason={stop_reason}) — falling back to text parse")
                        return thinking_block, None, json.loads(match.group())
                    except json.JSONDecodeError:
                        continue

    raise ValueError(
        f"No tool_use or text block found. "
        f"stop_reason={stop_reason}, block_types={block_types}"
    )


# ─── Hallucination helpers ────────────────────────────────────────────────────

def normalize_hallucinations(raw):
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        result = []
        for h in raw:
            if isinstance(h, dict) and h.get('field'):
                result.append(h)
            elif isinstance(h, str) and h.strip():
                result.append(h)
        return result
    return []


def tag_hallucinations(hallucinations, attempt_number):
    tagged = []
    for h in hallucinations:
        if isinstance(h, dict):
            h['attempt'] = attempt_number
            tagged.append(h)
        else:
            tagged.append(f"[Attempt {attempt_number}] {h}")
    return tagged


# ─── Reconciled metadata sanitizers ──────────────────────────────────────────

def sanitize_name_field(value, field_name, letter_id):
    if value is None:
        return None
    if isinstance(value, dict):
        if "name" not in value:
            print(f"[WARN] {field_name} object missing 'name' key for {letter_id} — setting to null")
            return None
        return value
    if isinstance(value, str) and value.strip():
        print(f"[WARN] {field_name} returned as plain string for {letter_id} — wrapping as object")
        return {"name": value.strip(), "lcnaf_uri": None}
    print(f"[WARN] {field_name} has unexpected type {type(value).__name__} for {letter_id} — setting to null")
    return None


def sanitize_subjects_field(value, letter_id):
    if not value:
        return []
    if not isinstance(value, list):
        print(f"[WARN] subjects is not a list for {letter_id} — defaulting to empty")
        return []
    labels = []
    for i, s in enumerate(value):
        if isinstance(s, dict):
            label = (s.get("label") or s.get("term") or "").strip()
            if label:
                labels.append(label)
            else:
                print(f"[WARN] subjects[{i}] dict has no label for {letter_id} — skipping")
        elif isinstance(s, str) and s.strip():
            labels.append(s.strip())
        else:
            print(f"[WARN] subjects[{i}] has unexpected format for {letter_id} — skipping")
    return labels


def sanitize_reconciled_metadata(metadata, letter_id):
    if not isinstance(metadata, dict):
        raise ValueError(f"reconciledMetadata is not a dict for {letter_id}: {type(metadata).__name__}")
    metadata["creator"]   = sanitize_name_field(metadata.get("creator"),   "creator",   letter_id)
    metadata["recipient"] = sanitize_name_field(metadata.get("recipient"), "recipient", letter_id)
    metadata["subjects"]  = sanitize_subjects_field(metadata.get("subjects"), letter_id)
    return metadata


# ─── Subject URI restoration ──────────────────────────────────────────────────

def merge_enriched_subjects(plain_labels, enriched_subjects, letter_id):
    if not enriched_subjects:
        print(f"[WARN] No enriched_subjects snapshot for {letter_id} — dropping all subjects")
        return []

    enriched_by_label = {}
    for s in enriched_subjects:
        if isinstance(s, dict):
            label = (s.get("label") or s.get("term") or "").strip().lower()
            if label:
                enriched_by_label[label] = s

    merged   = []
    restored = 0
    dropped  = 0
    for label in plain_labels:
        key = label.strip().lower()
        if key in enriched_by_label:
            merged.append(enriched_by_label[key])
            restored += 1
        else:
            print(f"[DROP] Reconciler-added subject '{label}' has no enriched URI for {letter_id} — dropped")
            dropped += 1

    print(
        f"[OK] merge_enriched_subjects for {letter_id}: "
        f"{restored}/{len(plain_labels)} restored, {dropped} dropped (no URI)"
    )
    return merged


def flatten_subjects_for_re_enrichment(metadata, letter_id):
    subjects = metadata.get("subjects", [])
    flat = []
    for s in subjects:
        if isinstance(s, dict):
            label = (s.get("label") or s.get("term") or "").strip()
            if label:
                flat.append(label)
        elif isinstance(s, str) and s.strip():
            flat.append(s.strip())

    removed = len(subjects) - len(flat)
    if removed:
        print(f"[INFO] flatten_subjects: dropped {removed} empty/malformed subjects for {letter_id}")

    metadata["subjects"]                  = flat
    metadata.pop("subjectsValidated",     None)
    metadata.pop("genreUri",              None)
    metadata.pop("genreResolutionSource", None)

    print(f"[INFO] Subjects flattened for re-enrichment ({letter_id}): {flat}")
    return metadata


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    # ── required inputs ───────────────────────────────────────────────────────
    reconcile_output_s3uri  = force_string(event.get('reconcileOutputS3Uri'))
    letter_id               = force_string(event.get('letterId')) or ''
    reconcile_attempt       = int(event.get('reconcileAttempt', 0))
    previous_hallucinations = normalize_hallucinations(event.get('previousHallucinations', []))
    enriched_subjects       = event.get('enrichedSubjects') or []
    if not isinstance(enriched_subjects, list):
        enriched_subjects = []

    # ── thinking + transcription URIs ─────────────────────────────────────────
    transcription_by_page_s3uri = force_string(event.get('transcriptionByPageS3Uri'))
    transcribe_thinking_s3uri   = force_string(event.get('transcribeThinkingS3Uri'))
    metadata_thinking_s3uri     = force_string(event.get('metadataThinkingS3Uri'))
    reconcile_thinking_s3uri    = force_string(event.get('reconcileThinkingS3Uri'))

    # ── pass-through URIs (set by MergeTranscriptions, unchanged here) ────────
    transcription_output_s3uri = force_string(event.get('transcriptionOutputS3Uri'))
    word_positions_s3uri       = force_string(event.get('wordPositionsS3Uri'))
    judge_input_s3uri          = force_string(event.get('judgeInputS3Uri'))
    judge_output_s3uri         = force_string(event.get('judgeOutputS3Uri'))

    if not reconcile_output_s3uri:
        raise ValueError(f"Missing required reconcileOutputS3Uri for {letter_id}")

    lid = safe_id(letter_id)

    # 1. Read reconcile Bedrock output from S3
    bucket, key = parse_s3_uri(reconcile_output_s3uri)
    raw = json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8'))
    print(f"[DEBUG] reconcile output keys: {list(raw.keys()) if isinstance(raw, dict) else type(raw)}")

    # 2. Extract thinking block, tool_use block, and reconciled metadata
    thinking_block, tool_use_block, tool_result = extract_thinking_and_tool_result(
        raw, expected_tool_name="submit_reconciled_metadata"
    )

    # 3. Parse fields
    reconciled_metadata    = tool_result.get("reconciledMetadata") or tool_result.get("metadata") or tool_result
    current_hallucinations = normalize_hallucinations(tool_result.get("hallucinations", []))

    # 4. Sanitize — normalises subjects to plain label strings
    reconciled_metadata = sanitize_reconciled_metadata(reconciled_metadata, letter_id)

    # 5. Tag current hallucinations with attempt number then accumulate
    reconcile_attempt  += 1
    tagged_current      = tag_hallucinations(current_hallucinations, attempt_number=reconcile_attempt)
    all_hallucinations  = previous_hallucinations + tagged_current

    has_new_hallucinations = len(current_hallucinations) > 0
    reconciled             = not has_new_hallucinations

    if has_new_hallucinations:
        reconciled_metadata = flatten_subjects_for_re_enrichment(reconciled_metadata, letter_id)
    else:
        plain_labels = reconciled_metadata.get("subjects", [])
        reconciled_metadata["subjects"] = merge_enriched_subjects(
            plain_labels, enriched_subjects, letter_id
        )

    # 6. Save reconcile thinking block to S3
    new_reconcile_thinking_s3uri = reconcile_thinking_s3uri
    if thinking_block:
        reasoning_key = f"intermediate/reasoning/{lid}_reconcile_attempt{reconcile_attempt}_reasoning.json"
        s3.put_object(
            Bucket=PIPELINE_BUCKET,
            Key=reasoning_key,
            Body=json.dumps(thinking_block, separators=(',', ':')),
            ContentType="application/json"
        )
        new_reconcile_thinking_s3uri = f"s3://{PIPELINE_BUCKET}/{reasoning_key}"
        print(f"[OK] Reconcile thinking block saved → {reasoning_key}")

    print(
        f"[OK] ReconcileParser for {letter_id}: "
        f"attempt={reconcile_attempt}, "
        f"new_hallucinations={len(current_hallucinations)}, "
        f"total_hallucinations={len(all_hallucinations)}, "
        f"reconciled={reconciled}, "
        f"subjects_flattened={has_new_hallucinations}, "
        f"thinking={'yes' if thinking_block else 'no'}"
    )

    return {
        "reconciledMetadata":       reconciled_metadata,
        "reconciled":               reconciled,
        "reconcileAttempt":         reconcile_attempt,
        "hallucinations":           all_hallucinations,
        "letterId":                 letter_id,
        "transcriptionByPageS3Uri": transcription_by_page_s3uri,
        "transcribeThinkingS3Uri":  transcribe_thinking_s3uri,
        "metadataThinkingS3Uri":    metadata_thinking_s3uri,
        "reconcileThinkingS3Uri":   new_reconcile_thinking_s3uri,
        # ── pass-throughs ─────────────────────────────────────────────────────
        "transcriptionOutputS3Uri": transcription_output_s3uri,
        "wordPositionsS3Uri":       word_positions_s3uri,
        "judgeInputS3Uri":          judge_input_s3uri,
        "judgeOutputS3Uri":         judge_output_s3uri,
    }