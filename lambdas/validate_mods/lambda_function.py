import boto3
import json
import re
from datetime import datetime

ssm = boto3.client('ssm', region_name='us-east-2')
s3  = boto3.client('s3',  region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"
SSM_PARAM_NAME  = "/cursive-pipeline/validation-rules"

JUNK_NAME_PATTERNS = [
    r'^(mr|mrs|ms|dr|prof|rev|sir|dear|dearest|your|my)\.?\s*$',
    r'^(friend|sir|madam|brother|sister|father|mother|uncle|aunt)\.?\s*$',
    r'^\d+$',
    r'^[^a-zA-Z]+$',
    r'^(unknown|none|null|n/a|na|-)$',
]

DEFAULT_RULES = {
    "date_min_year":       1700,
    "date_max_year":       2000,
    "min_abstract_length": 20,
    "min_subjects_count":  1,
    "required_fields":     ["name", "originInfo"],
}

# ── Transcription scoring constants ───────────────────────────────────────────
#
# Layer 1 — Quality tier cap: hard ceiling on transcriptionScore based on the
#           overall transcription quality label returned by the judge.
#
#   accurate           → no ceiling, transcriptionScore stands
#   minor_errors       → small misreads that don't change meaning; 0.88 cap
#   significant_errors → names / dates / numbers wrong; 0.62 cap,
#                        isValid set False (record needs human review)
#   unreliable         → major portions don't match; 0.30 cap, isValid False
#
# Layer 2 — Per-issue deductions: applied AFTER the tier cap so that a
#           record with many minor issues inside "minor_errors" still lands
#           lower than one with zero issues.
#
#   critical issue → -0.08 each  (e.g. sender/recipient name misread)
#   major issue    → -0.04 each  (e.g. date or number wrong)
#   minor issue    → -0.01 each  (e.g. single word misread, meaning preserved)
#   ceiling on total per-issue deduction: 0.20

TRANSCRIPTION_QUALITY_SCORE_CAP = {
    "accurate":           1.00,
    "minor_errors":       0.88,
    "significant_errors": 0.62,
    "unreliable":         0.30,
}

TRANSCRIPTION_ISSUE_DEDUCTION = {
    "critical": 0.08,
    "major":    0.04,
    "minor":    0.01,
}

TRANSCRIPTION_ISSUE_DEDUCTION_MAX = 0.20

# Quality levels that force isValid = False regardless of MODS score
BLOCKING_TRANSCRIPTION_QUALITY = {"significant_errors", "unreliable"}


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
    return re.sub(r'[^a-zA-Z0-9\-]', '_', str(value))


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


def read_s3_text(uri):
    bucket, key = parse_s3_uri(uri)
    return s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8')


def read_s3_json(uri):
    return json.loads(read_s3_text(uri))


def _s3_put(key, body, content_type="application/json"):
    if isinstance(body, str):
        body = body.encode('utf-8')
    s3.put_object(Bucket=PIPELINE_BUCKET, Key=key, Body=body, ContentType=content_type)
    print(f"[OK] → s3://{PIPELINE_BUCKET}/{key}")


def load_rules():
    try:
        response = ssm.get_parameter(Name=SSM_PARAM_NAME, WithDecryption=False)
        rules    = json.loads(response['Parameter']['Value'])
        print(f"[OK] Loaded validation rules from SSM: {SSM_PARAM_NAME}")
        return {**DEFAULT_RULES, **rules}
    except ssm.exceptions.ParameterNotFound:
        print(f"[WARN] SSM param {SSM_PARAM_NAME} not found — using defaults")
        return DEFAULT_RULES
    except Exception as e:
        print(f"[WARN] Failed to load SSM rules: {e} — using defaults")
        return DEFAULT_RULES


def is_junk_name(name):
    if not name:
        return False
    name_lower = name.strip().lower()
    for pattern in JUNK_NAME_PATTERNS:
        if re.match(pattern, name_lower, re.IGNORECASE):
            return True
    return False


def parse_year_from_date(date_val):
    if not date_val:
        return None
    date_str = str(date_val).strip()
    for pattern in [r'^(\d{4})-\d{2}-\d{2}$', r'^(\d{4})-\d{2}$', r'^(\d{4})$']:
        m = re.match(pattern, date_str)
        if m:
            return int(m.group(1))
    return None


# ─── MODS Structural Validation ───────────────────────────────────────────────

def validate_mods(mods_record, metadata_score, rules, page_count=1):
    """
    Validate MODS structure and adjust metadataScore only.
    Returns (critical_count, issues, adjusted_metadata_score).
    """
    issues   = []
    critical = []

    try:
        metadata_score = float(metadata_score)
    except (TypeError, ValueError):
        metadata_score = 0.0

    # 1. Required top-level fields
    for field in rules.get("required_fields", DEFAULT_RULES["required_fields"]):
        val = mods_record.get(field)
        if val is None or val == "" or val == [] or val == {}:
            issues.append({
                "field":    field,
                "severity": "critical",
                "message":  f"Required field '{field}' is missing or empty",
            })
            critical.append(field)

    # 2. Title check
    title_info = mods_record.get('titleInfo', {})
    title      = title_info.get('title') if isinstance(title_info, dict) else None
    if not title or len(str(title).strip()) < 3:
        issues.append({
            "field":    "titleInfo.title",
            "severity": "critical",
            "message":  "Title is missing or too short",
        })
        critical.append("titleInfo.title")
    elif not str(title).startswith("Letter to"):
        issues.append({
            "field":    "titleInfo.title",
            "severity": "minor",
            "message":  f"Title does not follow 'Letter to X from Y, Date' format: '{str(title)[:60]}'",
        })

    # 3. Date range check
    origin_info = mods_record.get('originInfo', {})
    date_val    = origin_info.get('dateCreated') if isinstance(origin_info, dict) else None
    if date_val:
        year = parse_year_from_date(date_val)
        if year is None:
            issues.append({
                "field":    "originInfo.dateCreated",
                "severity": "major",
                "message":  f"Date '{date_val}' could not be parsed to a year",
            })
        elif year < rules["date_min_year"] or year > rules["date_max_year"]:
            issues.append({
                "field":    "originInfo.dateCreated",
                "severity": "critical",
                "message":  (
                    f"Date year {year} outside expected range "
                    f"{rules['date_min_year']}–{rules['date_max_year']}"
                ),
            })
            critical.append("originInfo.dateCreated")
    else:
        issues.append({
            "field":    "originInfo.dateCreated",
            "severity": "major",
            "message":  "Date is missing",
        })

    # 4. Name / junk check
    names = mods_record.get('name', [])
    if not isinstance(names, list) or len(names) == 0:
        issues.append({
            "field":    "name",
            "severity": "critical",
            "message":  "No name entries found in metadataRecord",
        })
        critical.append("name")
    else:
        for name_entry in names:
            if not isinstance(name_entry, dict):
                continue
            name_part = name_entry.get('namePart', '')
            role      = name_entry.get('role', 'unknown')
            if is_junk_name(name_part):
                issues.append({
                    "field":    f"name[{role}]",
                    "severity": "critical",
                    "message":  f"Name '{name_part}' for role '{role}' looks like a title or junk",
                })
                critical.append(f"name[{role}]")
            elif not name_part:
                issues.append({
                    "field":    f"name[{role}]",
                    "severity": "major",
                    "message":  f"Name is empty for role '{role}'",
                })

    # 5. Subjects count check
    subjects = mods_record.get('subject', [])
    if not isinstance(subjects, list) or len(subjects) < rules["min_subjects_count"]:
        issues.append({
            "field":    "subject",
            "severity": "major",
            "message":  f"subjects[] has fewer than {rules['min_subjects_count']} entries",
        })

    # 6. Abstract check
    abstract = mods_record.get('abstract', '')
    if not abstract or len(str(abstract).strip()) < rules["min_abstract_length"]:
        issues.append({
            "field":    "abstract",
            "severity": "major",
            "message":  f"Abstract missing or too short (min {rules['min_abstract_length']} chars)",
        })

    # 7. Genre authority check
    genre = mods_record.get('genre', {})
    if isinstance(genre, dict):
        if not genre.get('valueURI'):
            issues.append({
                "field":    "genre.valueURI",
                "severity": "minor",
                "message":  f"Genre '{genre.get('label')}' could not be resolved to an AAT URI",
            })
    elif isinstance(genre, str):
        issues.append({
            "field":    "genre",
            "severity": "minor",
            "message":  "Genre is a plain string — expected AAT-enriched dict with valueURI",
        })

    # 8. LCNAF name authority check
    for name_entry in (names if isinstance(names, list) else []):
        if not isinstance(name_entry, dict):
            continue
        role = name_entry.get('role', 'unknown')
        if not name_entry.get('valueURI'):
            issues.append({
                "field":    f"name[{role}].valueURI",
                "severity": "minor",
                "message":  f"Name '{name_entry.get('namePart')}' (role={role}) has no LCNAF URI",
            })

    # 9. Subject URI check
    for s in (subjects if isinstance(subjects, list) else []):
        if not isinstance(s, dict):
            continue
        term = s.get('term') or s.get('label', '')
        if not s.get('valueURI'):
            issues.append({
                "field":    "subject.valueURI",
                "severity": "minor",
                "message":  f"Subject '{term}' has no URI",
            })

    # 10. Multi-page location check
    location  = mods_record.get('location', {})
    page_urls = location.get('urls', []) if isinstance(location, dict) else []
    if page_count > 1 and len(page_urls) < page_count:
        issues.append({
            "field":    "location.urls",
            "severity": "minor",
            "message":  (
                f"Expected {page_count} location URLs for {page_count}-page letter, "
                f"found {len(page_urls)}"
            ),
        })

    # 11. Adjust metadataScore from MODS issues
    critical_count = len(critical)
    major_count    = len([i for i in issues if i['severity'] == 'major'])
    minor_count    = len([i for i in issues if i['severity'] == 'minor'])

    if critical_count > 0:
        metadata_score = min(metadata_score, 0.5)
    elif major_count >= 3:
        metadata_score = min(metadata_score, 0.69)
    elif major_count >= 1:
        metadata_score = metadata_score * 0.9

    if minor_count > 0:
        authority_penalty = min(minor_count * 0.02, 0.10)
        metadata_score    = metadata_score * (1 - authority_penalty)

    return critical_count, issues, round(metadata_score, 4)


# ─── Transcription Validation ─────────────────────────────────────────────────

def validate_transcription(transcription_score, transcription_quality, transcription_issues):
    """
    Two-layer scoring applied to transcriptionScore only:
      Layer 1 — tier cap:          hard ceiling from quality label
      Layer 2 — per-issue deductions: stacked on top of capped score

    Example end-to-end:
      transcriptionScore from judge  = 0.91
      transcriptionQuality = "minor_errors"  → cap = 0.88  → score = 0.88
      2 minor issues  → -0.02                              → score = 0.86
      1 major issue   → -0.04                              → score = 0.82  (final)
    """
    t_issues   = []
    t_critical = []

    quality = (transcription_quality or "accurate").strip().lower()

    # Layer 1: apply tier cap to transcriptionScore
    cap               = TRANSCRIPTION_QUALITY_SCORE_CAP.get(quality, 1.0)
    transcription_score = min(transcription_score, cap)

    # Layer 2: per-issue deductions
    total_deduction = 0.0
    if isinstance(transcription_issues, list):
        for issue in transcription_issues:
            if not isinstance(issue, dict):
                continue
            severity  = issue.get('severity', 'minor')
            deduction = TRANSCRIPTION_ISSUE_DEDUCTION.get(severity, 0.01)
            total_deduction += deduction

            location = issue.get('location', '?')
            img_txt  = issue.get('imageText', '?')
            got_txt  = issue.get('transcribedText', '?')
            note     = issue.get('note', '')
            message  = (
                f"Transcription mismatch at {location}: "
                f"image=\"{img_txt}\" transcribed=\"{got_txt}\""
                + (f" — {note}" if note else "")
            )
            t_issues.append({
                "field":    f"transcription[{location}]",
                "severity": severity,
                "message":  message,
            })
            if severity == "critical":
                t_critical.append(location)

    # Clamp total per-issue deduction to ceiling
    total_deduction     = min(total_deduction, TRANSCRIPTION_ISSUE_DEDUCTION_MAX)
    transcription_score = max(0.0, transcription_score - total_deduction)

    # Surface quality-label issue for significant_errors / unreliable
    if quality in ("significant_errors", "unreliable"):
        sev = "critical" if quality == "unreliable" else "major"
        t_issues.append({
            "field":    "transcriptionQuality",
            "severity": sev,
            "message":  f"Overall transcription quality assessed as '{quality}'",
        })
        if quality == "unreliable":
            t_critical.append("transcriptionQuality")

    return len(t_critical), t_issues, round(transcription_score, 4)


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    metadata_record             = event.get('metadataRecord', {})
    letter_id                   = force_string(event.get('letterId'))
    reason                      = force_string(event.get('reason', ''))
    metadata_s3_uri             = force_string(event.get('metadataS3Uri'))
    mods_xml_s3uri              = force_string(event.get('modsXmlS3Uri'))
    transcription_output_s3uri  = force_string(event.get('transcriptionOutputS3Uri'))
    transcription_by_page_s3uri = force_string(event.get('transcriptionByPageS3Uri'))
    transcribe_thinking_s3uri   = force_string(event.get('transcribeThinkingS3Uri'))
    metadata_thinking_s3uri     = force_string(event.get('metadataThinkingS3Uri'))
    reconcile_thinking_s3uri    = force_string(event.get('reconcileThinkingS3Uri'))
    judge_thinking_s3uri        = force_string(event.get('judgeThinkingS3Uri'))
    word_positions_s3uri        = force_string(event.get('wordPositionsS3Uri'))
    judge_input_s3uri           = force_string(event.get('judgeInputS3Uri'))
    judge_output_s3uri          = force_string(event.get('judgeOutputS3Uri'))
    transcription_issues        = event.get('transcriptionIssues', [])
    transcription_quality       = force_string(event.get('transcriptionQuality')) or 'accurate'

    # ── Split scores from judge ───────────────────────────────────────────────
    metadata_score      = max(0.0, min(1.0, float(event.get('metadataScore',     0.0))))
    transcription_score = max(0.0, min(1.0, float(event.get('transcriptionScore', 0.0))))
    # finalScore is recomputed after both validation layers below

    if isinstance(metadata_record, list):
        metadata_record = metadata_record[0] if metadata_record else {}
    if not isinstance(transcription_issues, list):
        transcription_issues = []

    if not metadata_record or not letter_id:
        raise ValueError(
            f"Missing required input. letterId={letter_id}, "
            f"metadataRecord present={bool(metadata_record)}"
        )

    lid = safe_id(letter_id)

    # Derive page_count from transcription-by-page JSON in S3
    page_count = 1
    if transcription_by_page_s3uri:
        try:
            transcriptions_dict = read_s3_json(transcription_by_page_s3uri)
            page_count = len(transcriptions_dict) if isinstance(transcriptions_dict, dict) else 1
        except Exception as e:
            print(f"[WARN] Could not read transcriptions for page count: {e} — defaulting to 1")

    rules = load_rules()

    # Part 1: MODS structural validation — adjusts metadataScore only
    critical_count, mods_issues, metadata_score = validate_mods(
        metadata_record, metadata_score, rules, page_count=page_count
    )

    # Part 2: Transcription quality validation — adjusts transcriptionScore only
    t_critical_count, t_issues, transcription_score = validate_transcription(
        transcription_score, transcription_quality, transcription_issues
    )

    # Part 3: Recompute finalScore from the validated component scores
    final_score = round(metadata_score * 0.6 + transcription_score * 0.4, 2)

    # Merge all issues
    all_issues     = mods_issues + t_issues
    total_critical = critical_count + t_critical_count

    # isValid: no critical MODS issues AND transcription not in blocking set
    is_valid = (
        critical_count == 0
        and transcription_quality.lower() not in BLOCKING_TRANSCRIPTION_QUALITY
    )

    # Read and write final MODS XML
    mods_xml         = None
    final_mods_s3uri = mods_xml_s3uri
    if mods_xml_s3uri:
        try:
            mods_xml = read_s3_text(mods_xml_s3uri)
        except Exception as e:
            print(f"[WARN] Could not read modsXml from {mods_xml_s3uri}: {e}")

    if mods_xml:
        final_mods_key   = f"output/mods/{lid}_mods.xml"
        _s3_put(final_mods_key, mods_xml, content_type="application/xml")
        final_mods_s3uri = f"s3://{PIPELINE_BUCKET}/{final_mods_key}"

    # Read and write final metadata JSON
    final_json_s3uri = metadata_s3_uri
    final_json_key   = f"output/mods/{lid}_metadata.json"
    try:
        metadata_json_text = read_s3_text(metadata_s3_uri)
        _s3_put(final_json_key, metadata_json_text)
        final_json_s3uri = f"s3://{PIPELINE_BUCKET}/{final_json_key}"
    except Exception as e:
        print(f"[WARN] Could not copy metadata JSON to output: {e}")

    minor_count = len([i for i in all_issues if i['severity'] == 'minor'])
    major_count = len([i for i in all_issues if i['severity'] == 'major'])

    print(
        f"[OK] ValidateMODS for {letter_id}: "
        f"pages={page_count}, "
        f"metadata_score={metadata_score:.4f}, "
        f"transcription_score={transcription_score:.4f}, "
        f"final_score={final_score:.2f}, "
        f"critical={total_critical} (mods={critical_count} + transcription={t_critical_count}), "
        f"major={major_count}, minor={minor_count}, "
        f"transcriptionQuality={transcription_quality}, "
        f"isValid={is_valid}"
    )

    return {
        "letterId":                 letter_id,
        "metadataRecord":           metadata_record,
        "metadataS3Uri":            final_json_s3uri,
        "modsXmlS3Uri":             final_mods_s3uri,
        "metadataScore":            metadata_score,
        "transcriptionScore":       transcription_score,
        "finalScore":               final_score,
        "reason":                   reason,
        "isValid":                  is_valid,
        "criticalCount":            total_critical,
        "issues":                   all_issues,
        "fieldIssues":              all_issues,
        "transcriptionQuality":     transcription_quality,
        "transcriptionIssues":      transcription_issues,
        "transcriptionOutputS3Uri": transcription_output_s3uri,
        "transcriptionByPageS3Uri": transcription_by_page_s3uri,
        "transcribeThinkingS3Uri":  transcribe_thinking_s3uri,
        "metadataThinkingS3Uri":    metadata_thinking_s3uri,
        "reconcileThinkingS3Uri":   reconcile_thinking_s3uri,
        "judgeThinkingS3Uri":       judge_thinking_s3uri,
        "wordPositionsS3Uri":       word_positions_s3uri,
        "judgeInputS3Uri":          judge_input_s3uri,
        "judgeOutputS3Uri":         judge_output_s3uri,
        "intermediateBucket":       PIPELINE_BUCKET,
    }