import base64
import boto3
import json
import os
import re

s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"
THINKING_BUDGET = 12000
MAX_TOKENS      = 20000
PAGE_SEPARATOR  = "\n\n---\n\n"

MEDIA_TYPES = {
    '.jpg':  'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png':  'image/png',
    '.tif':  'image/tiff',
    '.tiff': 'image/tiff',
    '.gif':  'image/gif',
    '.webp': 'image/webp',
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def safe_id(value):
    return re.sub(r'[^a-zA-Z0-9\-]', '_', str(value))


def natural_sort_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]


def page_number_from_key(page_key):
    m = re.search(r'(\d+)', str(page_key))
    return int(m.group(1)) if m else 1


def get_media_type(image_key):
    ext = os.path.splitext(image_key)[1].lower()
    mt  = MEDIA_TYPES.get(ext)
    if not mt:
        raise ValueError(f"Unsupported image extension '{ext}' for key: {image_key}")
    return mt


def load_image_base64(bucket, key):
    data = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
    return base64.b64encode(data).decode('utf-8')


def _s3_put(key, body, content_type="application/json"):
    """Write body (str | bytes) to PIPELINE_BUCKET/key."""
    if isinstance(body, str):
        body = body.encode('utf-8')
    s3.put_object(Bucket=PIPELINE_BUCKET, Key=key, Body=body, ContentType=content_type)
    print(f"[OK] → s3://{PIPELINE_BUCKET}/{key}")


# ─── Thinking block validation ────────────────────────────────────────────────

def validate_thinking_block(block, label):
    """
    Validate that a thinking block is safe to re-inject into Bedrock.
    Must be a dict with type='thinking' and a non-empty signature.
    Returns the block if valid, None otherwise.
    """
    if not isinstance(block, dict):
        print(f"[WARN] {label}: thinkingBlock is not a dict ({type(block).__name__}) — skipping")
        return None
    if block.get('type') != 'thinking':
        print(f"[WARN] {label}: thinkingBlock type='{block.get('type')}' not 'thinking' — skipping")
        return None
    if not block.get('signature'):
        print(f"[WARN] {label}: thinkingBlock has no signature — cannot re-inject, skipping")
        return None
    if not block.get('thinking'):
        print(f"[WARN] {label}: thinkingBlock has empty thinking text — skipping")
        return None
    return block


# ─── Page normalisation ───────────────────────────────────────────────────────

def normalise_pages(event) -> list:
    """
    Step Functions Map state outputs the result array directly as the event.
    Handles raw array, {"pages": [...]} wrapped shape, and single-page runs.
    """
    if isinstance(event, list):
        return event
    if isinstance(event, dict):
        for key in ('pages', 'items', 'results', 'pageResults'):
            if key in event and isinstance(event[key], list):
                return event[key]
        # Single-page run — event IS one CursiveResultParser output
        if 'pageKey' in event and 'transcriptionText' in event:
            return [event]
    raise ValueError(f"Cannot extract page list from event type={type(event).__name__}")


def validate_page(page: dict, idx: int) -> None:
    required = ('pageKey', 'pageNumber', 'letterId', 'transcriptionText',
                 'wordPositions', 'imageKey')
    missing  = [f for f in required if f not in page or page[f] is None]
    if missing:
        raise ValueError(f"Page[{idx}] missing required fields: {missing}")


# ─── Metadata tool definition ─────────────────────────────────────────────────

METADATA_TOOL = {
    "name": "submit_metadata",
    "description": (
        "Submit structured MODS 3.7 metadata extracted from a historical "
        "handwritten letter. You will receive the letter as images — use them "
        "as the primary source. A transcription is provided for reference only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": (
                    "Format EXACTLY as: 'Letter to [Recipient First Last] from "
                    "[Creator First Last], [Month Day, Year]'. "
                    "Use the DISPLAY name only (e.g. 'A. J. Peeler', 'Louis L. McInnis'). "
                    "Do NOT include birth/death years, LCNAF qualifiers, or parenthetical "
                    "expansions in the title (e.g. NEVER '1838-1886' or '(Anderson James)'). "
                    "Use natural month name (e.g. June 10, 1880) — never ISO date in title. "
                    "If creator unknown: 'Letter to [Recipient] from Unknown, [Date]'. "
                    "If recipient unknown: 'Letter to Unknown from [Creator], [Date]'."
                )
            },
            "creator": {
                "type": ["object", "null"],
                "description": (
                    "The sender of the letter. "
                    "Examine the images carefully — look at the signature, letterhead, "
                    "body text, and any other part of the letter that clearly identifies "
                    "the sender. Prefer the handwritten signature for the exact name form. "
                    "Null only if no name can be determined from any part of the letter."
                ),
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "If LCNAF match confirmed: full LCNAF form with birth/death dates "
                            "e.g. 'Peeler, A. J. (Anderson James), 1838-1886'. "
                            "If no LCNAF match: bare inverted form only e.g. 'Whitlock, R. H.' — "
                            "no dates, no extra attributes."
                        )
                    },
                    "lcnaf_uri": {
                        "type": ["string", "null"],
                        "description": (
                            "Full LCNAF URI using HTTPS if match confirmed: "
                            "'https://id.loc.gov/authorities/names/[id]'. "
                            "Null if no confident LCNAF match."
                        )
                    }
                },
                "required": ["name", "lcnaf_uri"]
            },
            "creator_confidence": {
                "type": "string",
                "enum": ["high", "low"],
                "description": "high = name clearly legible/visible in image; low = partially legible or inferred."
            },
            "recipient": {
                "type": ["object", "null"],
                "description": (
                    "The recipient of the letter. "
                    "Examine the images carefully — look at the salutation, body text, "
                    "envelope address, or any other part of the letter that clearly "
                    "identifies who is being addressed. "
                    "Prefer the handwritten salutation for the exact name form. "
                    "Null only if no name can be determined from any part of the letter."
                ),
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "If LCNAF match confirmed: full LCNAF form with birth/death dates "
                            "e.g. 'McInnis, Louis Lowry, 1855-1933'. "
                            "If no LCNAF match: bare inverted form only e.g. 'Hand, J. T.' — "
                            "no dates, no extra attributes."
                        )
                    },
                    "lcnaf_uri": {
                        "type": ["string", "null"],
                        "description": (
                            "Full LCNAF URI using HTTPS if match confirmed: "
                            "'https://id.loc.gov/authorities/names/[id]'. "
                            "Null if no confident LCNAF match."
                        )
                    }
                },
                "required": ["name", "lcnaf_uri"]
            },
            "recipient_confidence": {
                "type": "string",
                "enum": ["high", "low"],
                "description": "high = name clearly legible/visible in image; low = partially legible or inferred."
            },
            "date": {
                "type": ["string", "null"],
                "description": (
                    "ISO 8601: YYYY-MM-DD, YYYY-MM, or YYYY. "
                    "Read the date line directly from the image. "
                    "If only year appears in the date line but body implies full date, infer it. "
                    "Null if not determinable."
                )
            },
            "place": {
                "type": ["string", "null"],
                "description": "City, State where the letter was written, read from the image. Null if not found."
            },
            "language": {
                "type": "string",
                "description": "ISO 639-2 code e.g. eng, fre, deu."
            },
            "genre": {
                "type": "string",
                "description": (
                    "A single genre term from the Getty Art & Architecture Thesaurus (AAT). "
                    "Always use: 'correspondence' for letters."
                )
            },
            "subjects": {
                "type": "array",
                "description": (
                    "Only include subjects directly evidenced by the letter content visible "
                    "in the images. Fewer high-quality subjects are better than padding. "
                    "Use type='topic' with authority='lcsh' for thematic LCSH headings. "
                    "Use type='name_entity' with authority='lcnaf' for named institutions or events. "
                    "LCNAF URIs: https://id.loc.gov/authorities/names/[id]. "
                    "LCSH URIs: http://id.loc.gov/authorities/subjects/[id]. "
                    "NEVER add: Lawyers, Hospitality, School field trips, Clergy unless "
                    "explicitly central to the letter content."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type":      {"type": "string", "enum": ["topic", "name_entity"]},
                        "authority": {"type": "string", "enum": ["lcsh", "lcnaf"]},
                        "label":     {"type": "string"},
                        "value_uri": {"type": ["string", "null"]}
                    },
                    "required": ["type", "authority", "label", "value_uri"]
                }
            },
            "abstract": {
                "type": "string",
                "description": (
                    "1-3 sentence narrative summary based on what you see in the images. "
                    "Include: who wrote to whom, from where, on what date, "
                    "and the key purpose or content of the letter."
                )
            },
            "extent": {
                "type": ["string", "null"],
                "description": "Number of leaves visible in the images e.g. '1 leaf', '2 leaves'."
            },
            "note": {
                "type": ["string", "null"],
                "description": (
                    "Observations about legibility, condition, archaic spelling, "
                    "or anything unusual visible in the images."
                )
            },
            "identifier": {
                "type": "string",
                "description": "S3 image key passed through from the pipeline."
            }
        },
        "required": [
            "title", "creator", "creator_confidence",
            "recipient", "recipient_confidence",
            "date", "place", "language", "genre",
            "subjects", "abstract", "extent", "note", "identifier"
        ]
    }
}


# ─── Metadata prompt ──────────────────────────────────────────────────────────

METADATA_PROMPT = """
You are a professional library cataloger trained in Library of Congress MODS 3.7,
LCSH, LCNAF, and Getty AAT vocabularies.

You will receive:
  1) The PAGE IMAGES of a historical handwritten letter — these are your PRIMARY source.
     Read the letter directly from the images. Trust what you see.
  2) A machine-generated transcription of the letter — use this as SUPPLEMENTARY
     context only (e.g. to confirm a hard-to-read word). If the transcription
     contradicts what you can clearly see in the image, trust the image.

Your task is to extract structured MODS 3.7 metadata from the letter.

══════════════════════════════════════════════════════
  HOW TO READ THE IMAGES
══════════════════════════════════════════════════════

  • Examine every part of each image: letterhead, date line, salutation,
    body text, signature, postscript, envelope address, stamps, annotations.
  • For multi-page letters, treat all pages as one continuous letter.
    Date and salutation typically appear on page 1; signature on the last page.
  • Zoom in mentally on cursive handwriting — read stroke by stroke if needed.

══════════════════════════════════════════════════════
  TITLE
══════════════════════════════════════════════════════

  Format EXACTLY as:
    "Letter to [Recipient First Last] from [Creator First Last], [Month Day, Year]"

  Use DISPLAY NAMES ONLY in the title — first and last name as they appear.
  Do NOT include birth/death years, LCNAF qualifiers, or parenthetical expansions.

  Examples:
    ✓  Letter to Louis L. McInnis from A. J. Peeler, June 10, 1880
    ✓  Letter to Louis L. McInnis from R. H. Whitlock, December 28, 1884
    ✗  Letter to Louis Lowry McInnis, 1855-1933 from A. J. Peeler, June 10, 1880   ← NEVER include birth/death dates
    ✗  Letter to McInnis from Peeler, A. J. (Anderson James), June 10, 1880   ← NEVER use LCNAF form in title
    ✗  Letter from Peeler to McInnis, 1880-06-10   ← wrong direction, wrong date format

══════════════════════════════════════════════════════
  CREATOR AND RECIPIENT
══════════════════════════════════════════════════════

  CREATOR (sender):
    • Primary source: the handwritten SIGNATURE visible in the image.
    • If no signature is visible, or to confirm/supplement it, look for the
      sender's name ANYWHERE else visible in the images:
        – Printed or handwritten letterhead
        – Body text (e.g. "I, Robert Whitlock, write to inform you...")
        – Return address or envelope
    • Null ONLY if the sender cannot be identified from ANY part of the images.

  RECIPIENT:
    • Primary source: the handwritten SALUTATION visible in the image
      (e.g. "Dear Prof. McInnis").
    • If the salutation contains only a title ("Dear Sir", "Dear Professor")
      with no name, look for the recipient ANYWHERE else in the images:
        – Body text referencing the reader by name
        – Envelope address or addressee line
    • Null ONLY if the recipient cannot be identified from ANY part of the images.

  NAME FORMAT:
    • LCNAF match confirmed → full LCNAF form with birth/death dates:
        "McInnis, Louis Lowry, 1855-1933"
        "Peeler, A. J. (Anderson James), 1838-1886"
    • No LCNAF match → bare inverted form, no dates, no extra attributes:
        "Whitlock, R. H."
    • lcnaf_uri must use HTTPS: "https://id.loc.gov/authorities/names/[id]"

  ⚠️  NEVER hallucinate a name. If uncertain → null.
  ⚠️  Printed letterhead lists an organisation — only use it if it clearly
      names the individual sender, not just the institution.

══════════════════════════════════════════════════════
  DATE
══════════════════════════════════════════════════════

  • Read the date line directly from the image.
  • Normalize to ISO 8601: YYYY-MM-DD, YYYY-MM, or YYYY.
  • If only year appears in the date line but the body implies a full date,
    infer it. Cross-validate month names character by character.

══════════════════════════════════════════════════════
  SUBJECTS — EVIDENCE-BASED ONLY
══════════════════════════════════════════════════════

  • Only include subjects directly evidenced by content visible in the images.
  • Fewer high-quality subjects are better than padding.
  • For named institutions/events → type="name_entity", authority="lcnaf".
  • For thematic headings → type="topic", authority="lcsh".
  • LCNAF URIs: https://id.loc.gov/authorities/names/[id]
  • LCSH URIs:  http://id.loc.gov/authorities/subjects/[id]
  • NEVER add: Lawyers, Hospitality, School field trips, Clergy
    unless explicitly central to the letter's purpose.

══════════════════════════════════════════════════════
  FEW-SHOT EXAMPLES
══════════════════════════════════════════════════════

  ── EXAMPLE 1 ──────────────────────────────────────
  Image shows: A. J. Peeler (Austin attorney) writes to Prof. L.L. McInnis.
  Signature visible: A.J. Peeler. Salutation visible: "Prof L.L. McInnis"

  CORRECT output:
  {
    "title": "Letter to Louis L. McInnis from A. J. Peeler, June 10, 1880",
    "creator": {"name": "Peeler, A. J. (Anderson James), 1838-1886",
                "lcnaf_uri": "https://id.loc.gov/authorities/names/no91007858"},
    "creator_confidence": "high",
    "recipient": {"name": "McInnis, Louis Lowry, 1855-1933",
                  "lcnaf_uri": "https://id.loc.gov/authorities/names/n2008161849"},
    "recipient_confidence": "high",
    "date": "1880-06-10",
    "place": "Austin, Texas",
    "language": "eng",
    "genre": "correspondence",
    "subjects": [
      {"type": "topic", "authority": "lcsh",
       "label": "Universities and colleges--Texas--College Station",
       "value_uri": "http://id.loc.gov/authorities/subjects/sh85141086"},
      {"type": "topic", "authority": "lcsh",
       "label": "College teachers--Texas",
       "value_uri": "http://id.loc.gov/authorities/subjects/sh85028378"}
    ],
    "abstract": "A brief personal letter from attorney A. J. Peeler of Austin, Texas to Professor Louis L. McInnis at College Station. Peeler accepts McInnis's invitation to stay with him during an upcoming college visit.",
    "extent": "1 leaf",
    "note": null,
    "identifier": "default-1.jpg"
  }

  ── EXAMPLE 2 ──────────────────────────────────────
  Two-page letter: page 1 image shows date line and "Dear Sir" salutation.
  Page 2 image shows body text "I trust you, President Gathright, will approve"
  and a signature at the bottom.

  Recipient = "Gathright" found in body text of page 2 image.
  Set extent to "2 leaves".

══════════════════════════════════════════════════════

You MUST call the submit_metadata tool with your structured extraction.
Do not respond with plain text.
"""


# ─── Metadata request builder ─────────────────────────────────────────────────

def build_metadata_request(sorted_image_keys, combined_transcription, primary_image_key):
    """
    Build the Bedrock InvokeModel request body for the metadata extraction pass.
    Images are embedded as base64.  Transcription is appended as a secondary
    reference block so the model can confirm hard-to-read words without
    over-relying on it.
    """
    content = []

    content.append({
        "type": "text",
        "text": (
            f"{METADATA_PROMPT}\n\n"
            f"imageKey (primary identifier): {primary_image_key}\n\n"
            f"The letter images follow below. "
            f"{'Each page is labelled.' if len(sorted_image_keys) > 1 else ''}"
        )
    })

    for page_num, image_key in sorted_image_keys:
        s3_key     = image_key if image_key.startswith("input/") else f"input/{image_key}"
        media_type = get_media_type(image_key)
        image_b64  = load_image_base64(PIPELINE_BUCKET, s3_key)

        if len(sorted_image_keys) > 1:
            content.append({"type": "text", "text": f"--- Page {page_num} ---"})

        content.append({
            "type": "image",
            "source": {
                "type":       "base64",
                "media_type": media_type,
                "data":       image_b64,
            }
        })
        print(f"[OK] Embedded image page {page_num}: {image_key} ({media_type})")

    content.append({
        "type": "text",
        "text": (
            f"\n\nSUPPLEMENTARY TRANSCRIPTION (machine-generated — use as secondary reference only):\n"
            f"<<<TRANSCRIPTION_START>>>\n"
            f"{combined_transcription}\n"
            f"<<<TRANSCRIPTION_END>>>\n\n"
            f"Now call submit_metadata with what you extracted primarily from the images above."
        )
    })

    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens":        MAX_TOKENS,
        "thinking": {
            "type":          "enabled",
            "budget_tokens": THINKING_BUDGET,
        },
        "tools":       [METADATA_TOOL],
        "tool_choice": {"type": "auto"},
        "messages": [
            {"role": "user", "content": content}
        ],
    }


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):

    # 1. Normalise event → sorted list of CursiveResultParser outputs
    pages = normalise_pages(event)
    if not pages:
        raise ValueError("Empty page list — nothing to merge")

    for idx, page in enumerate(pages):
        validate_page(page, idx)

    pages.sort(key=lambda p: page_number_from_key(p.get('pageKey', '')))

    letter_id = pages[0].get('letterId')
    if not letter_id:
        raise ValueError("letterId missing from page[0]")

    print(f"[INFO] Merging {len(pages)} page(s) for letter '{letter_id}'")
    lid = safe_id(letter_id)

    # 2. Merge all per-page data
    transcription_by_page = {}   # {page_num_int: text}
    all_word_positions    = []   # flat letter-level list
    # Store full thinking block dicts keyed by "page_N" string — NOT plain text
    # strings. Signatures must be preserved intact so CursiveMODSFormatter can
    # re-inject them into Bedrock.
    transcribe_thinking   = {}   # {"page_N": {type, thinking, signature}}
    image_keys_map        = {}   # {page_key: image_key}
    sorted_image_keys     = []   # [(page_num_int, image_key), ...]

    for page in pages:
        pk       = page.get('pageKey', '')
        page_num = page_number_from_key(pk)
        ik       = page.get('imageKey', '')

        text = page.get('transcriptionText', '').strip()
        if not text:
            print(f"[WARN] page_{page_num} has empty transcriptionText — including blank entry")
        transcription_by_page[page_num] = text

        # Pull the full block dict {type, thinking, signature} — CursiveResultParser
        # outputs 'thinkingBlock' as the complete Bedrock dict. Do NOT use 'thinkingText'
        # here — that is a logging-only convenience field and has no signature.
        think_block = page.get('thinkingBlock')
        validated   = validate_thinking_block(think_block, f"page_{page_num}")
        if validated:
            page_key_str = f"page_{page_num}"
            transcribe_thinking[page_key_str] = validated
            print(f"[OK] Thinking block with signature preserved for {page_key_str}")
        else:
            print(f"[WARN] No valid thinking block for page_{page_num} — "
                  f"judge will have reduced context for this page")

        for wp in page.get('wordPositions', []):
            wp['page'] = page_num
            all_word_positions.append(wp)

        image_keys_map[pk] = ik
        sorted_image_keys.append((page_num, ik))

    # Sort once — ascending page number
    all_word_positions.sort(key=lambda w: (w.get('page', 1), w.get('wordIndex', 0)))
    sorted_image_keys.sort(key=lambda x: x[0])

    primary_image_key    = sorted_image_keys[0][1]
    valid_thinking_count = len(transcribe_thinking)

    print(
        f"[OK] {letter_id} — merged {len(pages)} page(s), "
        f"{len(all_word_positions)} word positions, "
        f"thinking blocks with signatures: {valid_thinking_count}/{len(pages)}"
    )

    # 3. Build combined transcription string
    sorted_by_page = sorted(transcription_by_page.items())
    if len(sorted_by_page) == 1:
        combined_transcription = sorted_by_page[0][1]
    else:
        combined_transcription = PAGE_SEPARATOR.join(
            f"--- page_{pn} ---\n{text}"
            for pn, text in sorted_by_page
        )

    # 4. Build Bedrock metadata request
    metadata_request = build_metadata_request(
        sorted_image_keys,
        combined_transcription,
        primary_image_key,
    )

    # 5. S3 keys
    transcription_by_page_key  = f"intermediate/transcribe-output/{lid}_transcription_by_page.json"
    word_positions_key         = f"intermediate/word-positions/{lid}_word_positions.json"
    transcribe_thinking_key    = f"intermediate/reasoning/{lid}_transcribe_thinking.json"
    combined_transcription_key = f"intermediate/transcribe-output/{lid}_combined_transcription.txt"
    metadata_payload_key       = f"intermediate/payloads/{lid}_metadata_request.json"
    metadata_output_key        = f"intermediate/metadata-output/{lid}_metadata_result.json"
    reconcile_payload_key      = f"intermediate/reconcile-input/{lid}_reconcile_request.json"
    reconcile_output_key       = f"intermediate/reconcile-output/{lid}_reconcile_result.json"
    judge_payload_key          = f"intermediate/judge-input/{lid}_judge_request.json"
    judge_output_key           = f"intermediate/judge-output/{lid}_judge_result.json"

    # 6. Write all S3 artifacts
    _s3_put(
        transcription_by_page_key,
        json.dumps({str(k): v for k, v in transcription_by_page.items()},
                   ensure_ascii=False, separators=(',', ':'))
    )

    _s3_put(
        word_positions_key,
        json.dumps(all_word_positions, separators=(',', ':'))
    )
    print(f"    ({len(all_word_positions)} word positions)")

    # Saves {"page_1": {type, thinking, signature}, ...} — NOT {"1": "plain string"}
    _s3_put(
        transcribe_thinking_key,
        json.dumps(transcribe_thinking, ensure_ascii=False, separators=(',', ':'))
    )
    print(f"    ({valid_thinking_count} thinking blocks with signatures)")

    _s3_put(
        combined_transcription_key,
        combined_transcription,
        content_type="text/plain"
    )

    _s3_put(
        metadata_payload_key,
        json.dumps(metadata_request)
    )

    return {
        "letterId":                 letter_id,
        "pageCount":                len(pages),
        "primaryImageKey":          primary_image_key,
        "imageKeys":                image_keys_map,
        "transcriptionByPageS3Uri": f"s3://{PIPELINE_BUCKET}/{transcription_by_page_key}",
        "wordPositionsS3Uri":       f"s3://{PIPELINE_BUCKET}/{word_positions_key}",
        "transcribeThinkingS3Uri":  f"s3://{PIPELINE_BUCKET}/{transcribe_thinking_key}",
        "transcriptionOutputS3Uri": f"s3://{PIPELINE_BUCKET}/{combined_transcription_key}",
        "metadataInputS3Uri":       f"s3://{PIPELINE_BUCKET}/{metadata_payload_key}",
        "metadataOutputS3Uri":      f"s3://{PIPELINE_BUCKET}/{metadata_output_key}",
        "reconcileInputS3Uri":      f"s3://{PIPELINE_BUCKET}/{reconcile_payload_key}",
        "reconcileOutputS3Uri":     f"s3://{PIPELINE_BUCKET}/{reconcile_output_key}",
        "judgeInputS3Uri":          f"s3://{PIPELINE_BUCKET}/{judge_payload_key}",
        "judgeOutputS3Uri":         f"s3://{PIPELINE_BUCKET}/{judge_output_key}",
        "intermediateBucket":       PIPELINE_BUCKET,
    }