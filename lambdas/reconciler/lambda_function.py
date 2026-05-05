import boto3
import json
import base64
import re

s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"

THINKING_BUDGET = 10000
MAX_TOKENS      = 16000

MEDIA_TYPE_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",  "tiff": "image/tiff",
    "tif": "image/tiff", "gif":  "image/gif",
    "webp": "image/webp"
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

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


def load_thinking_text(uri, label):
    """
    Read a saved thinking block from S3 and return its text content.
    Returns None if the URI is missing or the block can't be read.
    """
    if not uri:
        return None
    try:
        block = json.loads(read_s3_text(uri))
        if isinstance(block, dict) and block.get('type') == 'thinking':
            text = block.get('thinking', '').strip()
            if text:
                print(f"[OK] Loaded {label} thinking text ({len(text)} chars)")
                return text
        print(f"[WARN] {label} thinking block has unexpected format — skipping")
        return None
    except Exception as e:
        print(f"[WARN] Could not load {label} thinking text from {uri}: {e}")
        return None


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


def load_image_b64(image_key):
    img_key    = f"input/{image_key}" if not image_key.startswith("input/") else image_key
    ext        = img_key.lower().split(".")[-1]
    media_type = MEDIA_TYPE_MAP.get(ext, "image/jpeg")
    obj        = s3.get_object(Bucket=PIPELINE_BUCKET, Key=img_key)
    b64        = base64.b64encode(obj["Body"].read()).decode("utf-8")
    return b64, media_type


def build_image_blocks(image_keys):
    if not image_keys:
        return []

    def page_sort_key(k):
        nums = re.findall(r'\d+', str(k))
        return int(nums[0]) if nums else 0

    if isinstance(image_keys, dict):
        ordered = sorted(image_keys.items(), key=lambda x: page_sort_key(x[0]))
    else:
        ordered = [(f"page_{i+1}", k) for i, k in enumerate(image_keys)]

    total = len(ordered)
    blocks = []
    for idx, (page_label, img_key) in enumerate(ordered, start=1):
        try:
            b64, media_type = load_image_b64(str(img_key))
            # Label each image so the model knows which page it's looking at
            blocks.append({
                "type": "text",
                "text": f"═══ PAGE {idx} OF {total} ({page_label}) ═══"
            })
            blocks.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": media_type,
                    "data":       b64
                }
            })
            print(f"[OK] Loaded image for {page_label}: {img_key}")
        except Exception as e:
            print(f"[WARN] Could not load image for {page_label} ({img_key}): {e}")

    return blocks


# ─── Reconcile tool schema ────────────────────────────────────────────────────

RECONCILE_TOOL = {
    "name": "submit_reconciled_metadata",
    "description": (
        "Submit fact-checked metadata for a historical handwritten letter. "
        "You are a FACT-CHECKER only — preserve all existing values unless "
        "they are clearly and demonstrably wrong based on the letter image. "
        "Never re-derive, re-normalize, or re-interpret fields from scratch."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reconciledMetadata": {
                "type": "object",
                "description": (
                    "The metadata with ONLY factual errors corrected. "
                    "All other fields must be returned exactly as received."
                ),
                "properties": {
                    "creator": {
                        "type": ["object", "null"],
                        "description": (
                            "Object with 'name' (inverted LCNAF form) and 'lcnaf_uri'. "
                            "Scan the ENTIRE letter for the sender — signature, body, "
                            "header, envelope notation, anywhere it is explicitly named. "
                            "Fix name only if it clearly contradicts what is written. "
                            "Preserve lcnaf_uri exactly as received — never alter it."
                        ),
                        "properties": {
                            "name":      {"type": "string"},
                            "lcnaf_uri": {"type": ["string", "null"]}
                        },
                        "required": ["name", "lcnaf_uri"]
                    },
                    "recipient": {
                        "type": ["object", "null"],
                        "description": (
                            "Object with 'name' (inverted LCNAF form) and 'lcnaf_uri'. "
                            "Scan the ENTIRE letter for the recipient — salutation, body, "
                            "address block, envelope notation, anywhere it is explicitly named. "
                            "Fix name only if it clearly contradicts what is written. "
                            "Preserve lcnaf_uri exactly as received — never alter it."
                        ),
                        "properties": {
                            "name":      {"type": "string"},
                            "lcnaf_uri": {"type": ["string", "null"]}
                        },
                        "required": ["name", "lcnaf_uri"]
                    },
                    "date":     {"type": ["string", "null"]},
                    "place":    {"type": ["string", "null"]},
                    "language": {"type": ["string", "null"]},
                    "abstract": {"type": ["string", "null"]},
                    "subjects": {
                        "type": "array",
                        "description": (
                            "Output subjects as PLAIN LABEL STRINGS ONLY — "
                            "e.g. ['Frontier and pioneer life', 'Women--Correspondence']. "
                            "Do NOT output objects with type/authority/value_uri. "
                            "Preserve all existing subject labels. Never remove a subject. "
                            "Only correct a label string if it visibly contradicts the letter content. "
                            "You may add a new plain label string if an important subject is clearly missing."
                        ),
                        "items": {"type": "string"}
                    },
                    "extent": {"type": ["string", "null"]},
                    "note":   {"type": ["string", "null"]}
                }
            },
            "hallucinations": {
                "type": "array",
                "description": (
                    "ONLY fields where the input metadata contains a value that "
                    "directly contradicts what is visually present in the letter image(s). "
                    "Do NOT report fields that are inferred, normalized, or interpreted — "
                    "only report clear factual errors visible in the image."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "field":          {"type": "string"},
                        "wrongValue":     {"type": "string"},
                        "correctedValue": {"type": ["string", "null"]},
                        "reason":         {"type": "string"}
                    },
                    "required": ["field", "wrongValue", "correctedValue", "reason"]
                }
            },
            "reconciled": {
                "type": "boolean",
                "description": "True if no clear factual errors were found."
            }
        },
        "required": ["reconciledMetadata", "hallucinations", "reconciled"]
    }
}


# ─── Reconcile prompt ─────────────────────────────────────────────────────────

def build_reconcile_prompt(transcription_text, raw_metadata,
                            reconcile_attempt, previous_hallucinations,
                            page_count, prior_thinking_context=""):
    page_note = (
        f"This letter spans {page_count} page(s). "
        f"All page images are provided above in order.\n"
        if page_count > 1 else ""
    )

    hallucination_block = ""
    if reconcile_attempt > 0 and previous_hallucinations:
        issue_lines = []
        for h in previous_hallucinations:
            if isinstance(h, dict):
                field       = h.get('field', 'unknown')
                wrong_value = h.get('wrongValue', '?')
                corrected   = h.get('correctedValue', 'null')
                reason      = h.get('reason', '')
                attempt     = h.get('attempt', '?')
                issue_lines.append(
                    f"  - [Attempt {attempt}] Field '{field}': "
                    f"was '{wrong_value}' → corrected to '{corrected}' — {reason}"
                )
            else:
                issue_lines.append(f"  - {h}")
        issues = "\n".join(issue_lines)
        hallucination_block = f"""
⚠️  PREVIOUS RECONCILIATION ATTEMPT {reconcile_attempt} FOUND THESE ERRORS.
Verify they are STILL correctly fixed and do NOT reintroduce them:

{issues}

"""

    return f"""You are a FACT-CHECKER for historical handwritten letter metadata.
Your ONLY job is to look at the letter image(s) and verify the factual fields.
You are NOT re-extracting metadata. You are NOT re-interpreting the letter.
{page_note}{hallucination_block}
YOUR MANDATE — read carefully:

YOU MAY ONLY CHANGE A FIELD IF:
  The value directly and visibly contradicts what is written in the letter image(s).

  Example — name wrong:
    creator.name="John Smith" but the letter clearly identifies the sender as "James Brown"
    → fix name to "Brown, James". Preserve lcnaf_uri exactly as received.

  Example — date month wrong:
    date="1880-01-10" but letter clearly shows "June 10th 1880"
    → fix to ISO 8601 "1880-06-10". Never revert to raw handwritten format.

  Example — date digits wrong (European format):
    date="1915-04-03" but letter clearly shows "4.3.15" (German DD.MM.YY)
    → March 4th → fix to ISO 8601 "1915-03-04".

YOU MUST NEVER:
  - Null out a field just because the exact phrase is not verbatim in the transcription.
  - Revert a normalized date to raw handwritten format — dates are always ISO 8601.
  - Remove subjects or reduce the subject list.
  - Null an abstract — it is an interpretive summary, always preserve it.
  - Re-derive or re-normalize any field from scratch.
  - Add new field values that were not already present (except subjects — see below).

FIELD-BY-FIELD RULES:

  creator:    Scan the ENTIRE letter — signature, body text, header, envelope notation,
              or anywhere the sender is clearly and explicitly named.
              Fix name.name only if it clearly contradicts what is written anywhere in the letter.
              Always preserve lcnaf_uri exactly as received — never alter it.
              If the sender cannot be identified anywhere → preserve whatever was passed in.

  recipient:  Scan the ENTIRE letter — salutation, body text, address block, envelope
              notation, or anywhere the recipient is clearly and explicitly named.
              Fix name.name only if it clearly contradicts what is written anywhere in the letter.
              Always preserve lcnaf_uri exactly as received — never alter it.
              If the recipient cannot be identified anywhere → preserve whatever was passed in.

  date:       Keep as ISO 8601. Fix ONLY if the date digits are clearly different in the image.
              Always correct TO ISO 8601 — never revert to raw handwritten format.
              Cross-validate month names character by character.

  place:      Fix only if image shows a clearly different location than recorded.

  language:   Fix only if you can clearly see the letter is in a different language.

  abstract:   NEVER null or change. It is an interpretive summary — always preserve it.

  subjects:   Output as PLAIN LABEL STRINGS — e.g. "Frontier and pioneer life".
              Do NOT output objects. Do NOT copy type/authority/value_uri from input.
              Preserve all existing subject labels — never drop one.
              Only correct a label string if it visibly contradicts the letter content.
              You MAY add a new plain label string if an important subject is clearly missing.
              URI resolution happens downstream — your job is labels only.

  extent:     Fix only if clearly wrong (e.g. says "2 leaves" but image shows 1 page).

  note:       Preserve as-is unless it contains a clearly verifiable factual error.

IF EVERYTHING IS CORRECT:
  Return reconciledMetadata with subjects as plain label strings (extracted from the
  input subject objects), empty hallucinations array, reconciled=true.

{prior_thinking_context}TRANSCRIPTION (for reference only — image(s) take priority for factual verification):
{transcription_text}

CURRENT METADATA TO FACT-CHECK:
{json.dumps(raw_metadata, indent=2)}

You MUST call the submit_reconciled_metadata tool with your fact-checked metadata.
Do not respond with plain text."""


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    letter_id               = event.get('letterId', '')
    image_keys              = event.get('imageKeys', {})
    raw_metadata            = event.get('rawMetadata', {})
    reconcile_input_s3uri   = event.get('reconcileInputS3Uri')
    reconcile_attempt       = int(event.get('reconcileAttempt', 0))
    previous_hallucinations = normalize_hallucinations(event.get('previousHallucinations', []))

    transcription_output_s3uri  = event.get('transcriptionOutputS3Uri')
    transcription_by_page_s3uri = event.get('transcriptionByPageS3Uri')
    transcribe_thinking_s3uri   = event.get('transcribeThinkingS3Uri')
    metadata_thinking_s3uri     = event.get('metadataThinkingS3Uri')
    reconcile_thinking_s3uri    = event.get('reconcileThinkingS3Uri')
    word_positions_s3uri        = event.get('wordPositionsS3Uri')
    judge_input_s3uri           = event.get('judgeInputS3Uri')
    judge_output_s3uri          = event.get('judgeOutputS3Uri')

    if isinstance(letter_id, list):
        letter_id = letter_id[0] if letter_id else ''
    if isinstance(raw_metadata, list):
        raw_metadata = raw_metadata[0] if raw_metadata else {}

    if not raw_metadata:
        raise ValueError(f"Missing rawMetadata for {letter_id}")

    # 1. Read combined transcription text from S3
    if not transcription_output_s3uri:
        raise ValueError(f"Missing transcriptionOutputS3Uri for {letter_id}")
    transcription_text = read_s3_text(transcription_output_s3uri)

    page_count = len(image_keys) if isinstance(image_keys, dict) and image_keys else 1

    # 2. Load prior thinking text from transcribe + metadata stages
    thinking_sections = []
    for uri, label in [
        (transcribe_thinking_s3uri, "Transcription"),
        (metadata_thinking_s3uri,   "Metadata extraction"),
    ]:
        text = load_thinking_text(uri, label)
        if text:
            thinking_sections.append(f"── {label} reasoning ──\n{text}")
    if reconcile_attempt > 0 and reconcile_thinking_s3uri:
        text = load_thinking_text(reconcile_thinking_s3uri, "Prior reconciliation")
        if text:
            thinking_sections.append(f"── Prior reconciliation reasoning ──\n{text}")

    prior_thinking_context = ""
    if thinking_sections:
        joined = "\n\n".join(thinking_sections)
        prior_thinking_context = (
            f"PRIOR REASONING FROM EARLIER PIPELINE STAGES (for context only — "
            f"image(s) take priority):\n{joined}\n\n"
        )

    # 3. Build prompt
    prompt = build_reconcile_prompt(
        transcription_text,
        raw_metadata,
        reconcile_attempt,
        previous_hallucinations,
        page_count,
        prior_thinking_context
    )

    # 4. Load all page images
    image_blocks = build_image_blocks(image_keys)
    if not image_blocks:
        print(f"[WARN] No images loaded for {letter_id} — reconciler will work from transcription only")

    # 5. Assemble messages
    user_content = image_blocks + [{"type": "text", "text": prompt}]
    messages = [{"role": "user", "content": user_content}]

    # 6. Build Bedrock payload
    bedrock_payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens":        MAX_TOKENS,
        "thinking": {
            "type":          "enabled",
            "budget_tokens": THINKING_BUDGET
        },
        "tools":       [RECONCILE_TOOL],
        "tool_choice": {"type": "auto"},
        "messages":    messages
    }

    # 7. Write payload to S3
    _, key = parse_s3_uri(reconcile_input_s3uri)
    s3.put_object(
        Bucket=PIPELINE_BUCKET,
        Key=key,
        Body=json.dumps(bedrock_payload),
        ContentType="application/json"
    )

    print(
        f"[OK] Reconciler payload written for {letter_id}: "
        f"pages={page_count}, "
        f"images_loaded={len(image_blocks)}, "
        f"attempt={reconcile_attempt}, "
        f"prior_hallucinations={len(previous_hallucinations)}, "
        f"prior_thinking_stages={len(thinking_sections)}"
    )

    return {
        "reconcileInputS3Uri":      reconcile_input_s3uri,
        "letterId":                 letter_id,
        "reconcileAttempt":         reconcile_attempt,
        "transcriptionByPageS3Uri": transcription_by_page_s3uri,
        "transcribeThinkingS3Uri":  transcribe_thinking_s3uri,
        "metadataThinkingS3Uri":    metadata_thinking_s3uri,
        "reconcileThinkingS3Uri":   reconcile_thinking_s3uri,
        "transcriptionOutputS3Uri": transcription_output_s3uri,
        "wordPositionsS3Uri":       word_positions_s3uri,
        "judgeInputS3Uri":          judge_input_s3uri,
        "judgeOutputS3Uri":         judge_output_s3uri
    }