import boto3
import json
import re


s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"

REQUIRED_FIELDS = ["title", "creator", "recipient", "date", "genre", "subjects", "abstract"]
OPTIONAL_FIELDS = ["creator_confidence", "recipient_confidence", "place", "language", "extent", "note", "identifier"]
ALLOWED_FIELDS  = set(REQUIRED_FIELDS + OPTIONAL_FIELDS)

BANNED_SUBJECT_LABELS = {
    "letters", "correspondence", "manuscripts",
    "documents", "history", "writing",
    "hospitality", "school field trips"
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def force_string(value):
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return str(value)


def safe_id(value):
    return re.sub(r'[^a-zA-Z0-9\-]', '_', value)


def parse_s3_uri(uri):
    if isinstance(uri, list):
        uri = uri[0] if uri else None
    if not uri:
        raise ValueError("S3 URI is empty or None")
    uri = str(uri).strip()
    if not uri.startswith('s3://'):
        raise ValueError(f"Invalid S3 URI: {uri}")
    parts = uri[5:].split('/', 1)
    if len(parts) != 2:
        raise ValueError(f"Could not parse bucket/key from URI: {uri}")
    return parts[0], parts[1]


def read_s3_json(uri):
    if not uri:
        return None
    bucket, key = parse_s3_uri(uri)
    return json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8'))


def extract_json_from_text(text):
    text = re.sub(r'```(?:json)?\s*', '', text).strip()
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise ValueError("No valid JSON object found in Claude response")
    return json.loads(match.group())


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
                print(f"[WARN] Expected tool '{expected_tool_name}', got '{block.get('name')}'")
            tool_use_block = block

    if tool_use_block:
        tool_input = tool_use_block.get('input')
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
                print(f"[WARN] tool_use not invoked (stop_reason={stop_reason}) — falling back to text parse")
                return thinking_block, None, extract_json_from_text(text)

    raise ValueError(
        f"No tool_use or text block found. "
        f"stop_reason={stop_reason}, block_types={block_types}"
    )


# ─── Validation ───────────────────────────────────────────────────────────────

def validate_name_object(obj, field_name):
    issues = []
    if obj is None:
        return issues
    if not isinstance(obj, dict):
        issues.append(f"{field_name} must be an object with 'name' and 'lcnaf_uri', got: {type(obj).__name__}")
        return issues
    if "name" not in obj:
        issues.append(f"{field_name}.name is missing")
    if "lcnaf_uri" not in obj:
        issues.append(f"{field_name}.lcnaf_uri is missing")
    else:
        uri = obj.get("lcnaf_uri")
        if uri and isinstance(uri, str) and uri.startswith("http://id.loc.gov/authorities/names/"):
            issues.append(f"{field_name}.lcnaf_uri uses http:// — should be https://")
    return issues


def validate_subjects(subjects):
    issues = []
    if subjects is None:
        issues.append("subjects is null — expected array")
        return issues
    if not isinstance(subjects, list):
        issues.append("subjects must be an array")
        return issues
    for i, s in enumerate(subjects):
        if not isinstance(s, dict):
            issues.append(f"subjects[{i}] must be an object, got: {type(s).__name__}")
            continue
        for required_key in ["type", "authority", "label", "value_uri"]:
            if required_key not in s:
                issues.append(f"subjects[{i}] missing key: '{required_key}'")
        label = s.get("label", "")
        if any(b == label.strip().lower() for b in BANNED_SUBJECT_LABELS):
            issues.append(f"subjects[{i}] contains banned generic label: '{label}'")
        s_type = s.get("type")
        if s_type not in ("topic", "name_entity"):
            issues.append(f"subjects[{i}].type must be 'topic' or 'name_entity', got: '{s_type}'")
        authority = s.get("authority")
        if authority not in ("lcsh", "lcnaf"):
            issues.append(f"subjects[{i}].authority must be 'lcsh' or 'lcnaf', got: '{authority}'")
    return issues


def validate_raw_metadata(raw):
    issues = []
    for field in REQUIRED_FIELDS:
        if field not in raw:
            issues.append(f"Missing key: {field}")
    title = raw.get("title", "")
    if isinstance(title, str) and title and not title.startswith("Letter to"):
        issues.append(f"title must start with 'Letter to ...', got: '{title[:60]}'")
    genre = raw.get("genre")
    if not genre or not isinstance(genre, str) or not genre.strip():
        issues.append("genre is missing or empty — expected AAT preferred label string")
    issues += validate_name_object(raw.get("creator"),   "creator")
    issues += validate_name_object(raw.get("recipient"), "recipient")
    issues += validate_subjects(raw.get("subjects"))
    for conf_field in ["creator_confidence", "recipient_confidence"]:
        val = raw.get(conf_field)
        if val is not None and val not in ("high", "low"):
            issues.append(f"{conf_field} must be 'high' or 'low' — got: {val}")
    return len(issues) == 0, issues


# ─── Sanitize ─────────────────────────────────────────────────────────────────

def normalize_lcnaf_uri(uri):
    if uri and isinstance(uri, str):
        return uri.replace("http://id.loc.gov/authorities/names/",
                           "https://id.loc.gov/authorities/names/")
    return uri


def sanitize_name_object(obj, field_name, letter_id):
    if obj is None:
        return None
    if not isinstance(obj, dict):
        print(f"[WARN] {field_name} is not an object for {letter_id} — setting to null")
        return None
    if obj.get("lcnaf_uri"):
        obj["lcnaf_uri"] = normalize_lcnaf_uri(obj["lcnaf_uri"])
    return obj


def sanitize_subjects(subjects, letter_id):
    if subjects is None:
        print(f"[WARN] subjects is null for {letter_id} — defaulting to empty list")
        return []
    if subjects and isinstance(subjects, str):
        print(f"[WARN] subjects is a string for {letter_id} — wrapping as topic object")
        wrapped = []
        for s in subjects:
            if not any(b == s.strip().lower() for b in BANNED_SUBJECT_LABELS):
                wrapped.append({
                    "type":      "topic",
                    "authority": "lcsh",
                    "label":     s.strip(),
                    "value_uri": None
                })
        return wrapped
    cleaned = []
    for s in subjects:
        if not isinstance(s, dict):
            continue
        label = s.get("label", "")
        if any(b == label.strip().lower() for b in BANNED_SUBJECT_LABELS):
            print(f"[WARN] Removed banned subject for {letter_id}: '{label}'")
            continue
        uri = s.get("value_uri")
        if uri and s.get("authority") == "lcnaf":
            s["value_uri"] = uri.replace("http://id.loc.gov/authorities/names/",
                                         "https://id.loc.gov/authorities/names/")
        cleaned.append(s)
    return cleaned


def sanitize_raw_metadata(raw, letter_id):
    raw["creator"]   = sanitize_name_object(raw.get("creator"),   "creator",   letter_id)
    raw["recipient"] = sanitize_name_object(raw.get("recipient"), "recipient", letter_id)
    raw["subjects"]  = sanitize_subjects(raw.get("subjects"), letter_id)
    if raw.get("genre"):
        raw["genre"] = raw["genre"].strip().lower()
    raw.setdefault("creator_confidence",   "low")
    raw.setdefault("recipient_confidence", "low")
    raw.setdefault("place",    None)
    raw.setdefault("language", "eng")
    raw.setdefault("extent",   None)
    raw.setdefault("note",     None)
    raw["identifier"] = letter_id
    return raw


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    metadata_output_s3uri        = force_string(event.get('metadataOutputS3Uri'))
    letter_id                    = force_string(event.get('letterId'))
    page_count                   = event.get('pageCount', 1)
    primary_image_key            = force_string(event.get('primaryImageKey', ''))
    image_keys                   = event.get('imageKeys', {})
    # ── renamed from MergeTranscriptions ──
    transcription_by_page_s3uri  = force_string(event.get('transcriptionByPageS3Uri'))
    transcribe_thinking_s3uri    = force_string(event.get('transcribeThinkingS3Uri'))
    # ── pass-through URIs ──
    transcription_output_s3uri   = force_string(event.get('transcriptionOutputS3Uri'))
    word_positions_s3uri         = force_string(event.get('wordPositionsS3Uri'))
    reconcile_input_s3uri        = force_string(event.get('reconcileInputS3Uri'))
    reconcile_output_s3uri       = force_string(event.get('reconcileOutputS3Uri'))
    judge_input_s3uri            = force_string(event.get('judgeInputS3Uri'))
    judge_output_s3uri           = force_string(event.get('judgeOutputS3Uri'))

    if not metadata_output_s3uri or not letter_id:
        raise ValueError(
            f"Missing required input. "
            f"metadataOutputS3Uri={metadata_output_s3uri}, letterId={letter_id}"
        )

    lid = safe_id(letter_id)

    # Load transcriptionByPage for page list logging only
    transcription_by_page = read_s3_json(transcription_by_page_s3uri) or {}

    # 1. Read metadata Bedrock output from S3
    bucket, key = parse_s3_uri(metadata_output_s3uri)
    raw = json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8'))

    # 2. Extract thinking block, tool_use block, and metadata
    thinking_block, tool_use_block, raw_metadata = extract_thinking_and_tool_result(
        raw, expected_tool_name="submit_metadata"
    )

    # 3. Validate schema
    is_valid, issues = validate_raw_metadata(raw_metadata)
    if not is_valid:
        print(f"[WARN] rawMetadata schema issues for {letter_id}: {issues}")

    # 4. Sanitize and normalize
    raw_metadata = sanitize_raw_metadata(raw_metadata, letter_id)

    # 5. Save rawMetadata to S3
    raw_metadata_key = f"intermediate/metadata-output/{lid}_raw_metadata.json"
    s3.put_object(
        Bucket=PIPELINE_BUCKET,
        Key=raw_metadata_key,
        Body=json.dumps(raw_metadata, indent=2),
        ContentType="application/json"
    )

    # 6. Save thinking block to S3 — too large to return inline
    metadata_thinking_s3uri = None
    if thinking_block:
        metadata_thinking_key = f"intermediate/reasoning/{lid}_metadata_reasoning.json"
        s3.put_object(
            Bucket=PIPELINE_BUCKET,
            Key=metadata_thinking_key,
            Body=json.dumps(thinking_block, separators=(',', ':')),
            ContentType="application/json"
        )
        metadata_thinking_s3uri = f"s3://{PIPELINE_BUCKET}/{metadata_thinking_key}"
        print(f"[OK] Metadata thinking block saved → {metadata_thinking_key}")

    print(
        f"[OK] rawMetadata parsed for {letter_id} — "
        f"pages={list(transcription_by_page.keys())}, "
        f"schemaValid={is_valid}, "
        f"subjects={len(raw_metadata.get('subjects', []))}, "
        f"genre={raw_metadata.get('genre')}, "
        f"thinking={'yes' if thinking_block else 'no'}"
    )

    return {
        "letterId":                      letter_id,
        "pageCount":                     page_count,
        "primaryImageKey":               primary_image_key,
        "imageKeys":                     image_keys,
        "rawMetadata":                   raw_metadata,
        "rawMetadataS3Uri":              f"s3://{PIPELINE_BUCKET}/{raw_metadata_key}",
        "metadataThinkingS3Uri":         metadata_thinking_s3uri,
        "transcriptionByPageS3Uri":      transcription_by_page_s3uri,
        "transcribeThinkingS3Uri":       transcribe_thinking_s3uri,
        "transcriptionOutputS3Uri":      transcription_output_s3uri,
        "wordPositionsS3Uri":            word_positions_s3uri,
        "reconcileInputS3Uri":           reconcile_input_s3uri,
        "reconcileOutputS3Uri":          reconcile_output_s3uri,
        "judgeInputS3Uri":               judge_input_s3uri,
        "judgeOutputS3Uri":              judge_output_s3uri,
        "intermediateBucket":            PIPELINE_BUCKET,
        "schemaValid":                   is_valid,
        "schemaIssues":                  issues
    }