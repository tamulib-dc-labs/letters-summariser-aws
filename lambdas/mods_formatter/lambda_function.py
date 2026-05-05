import boto3
import base64
import json
import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"

THINKING_BUDGET = 10000
MAX_TOKENS      = 16000

JUNK_NAME_PATTERNS = re.compile(
    r'^(mr|mrs|ms|dr|prof|hon|col|gen|rev|sir|lady|lord)\.?$',
    re.IGNORECASE
)

MEDIA_TYPE_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",  "tiff": "image/tiff",
    "tif": "image/tiff", "gif":  "image/gif",
    "webp": "image/webp",
}

MARCRELATOR_CREATOR   = "http://id.loc.gov/vocabulary/relators/cre"
MARCRELATOR_RECIPIENT = "http://id.loc.gov/vocabulary/relators/rcp"

# Default genre URI from Getty AAT for "correspondence"
AAT_CORRESPONDENCE_URI = "http://vocab.getty.edu/aat/300026877"


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
    if not uri:
        return None
    bucket, key = parse_s3_uri(uri)
    return s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8')


def read_s3_json(uri):
    text = read_s3_text(uri)
    return json.loads(text) if text else None


def read_s3_json_optional(uri):
    """Like read_s3_json but returns None (not raises) when uri is absent."""
    if not uri:
        return None
    try:
        return read_s3_json(uri)
    except Exception as e:
        print(f"[WARN] Could not read optional S3 JSON from {uri}: {e}")
        return None


def _s3_put(key, body, content_type="application/json"):
    """Write body (str | bytes) to PIPELINE_BUCKET/key."""
    if isinstance(body, str):
        body = body.encode('utf-8')
    s3.put_object(Bucket=PIPELINE_BUCKET, Key=key, Body=body, ContentType=content_type)
    print(f"[OK] → s3://{PIPELINE_BUCKET}/{key}")


def _s3_put_json(key, obj, indent=None):
    """Serialise obj to JSON and write to PIPELINE_BUCKET/key."""
    _s3_put(key, json.dumps(obj, indent=indent, ensure_ascii=False))


def normalize_date(raw_date):
    if not raw_date or str(raw_date).strip().lower() in ('null', 'none', ''):
        return None
    raw = str(raw_date).strip()
    if re.match(r'^\d{4}(-\d{2}(-\d{2})?)?$', raw):
        return raw
    formats = [
        ("%B %d, %Y", "%Y-%m-%d"), ("%b %d, %Y", "%Y-%m-%d"),
        ("%d %B %Y",  "%Y-%m-%d"), ("%d %b %Y",  "%Y-%m-%d"),
        ("%B %Y",     "%Y-%m"),    ("%b %Y",      "%Y-%m"),
        ("%m/%d/%Y",  "%Y-%m-%d"), ("%d/%m/%Y",   "%Y-%m-%d"),
        ("%Y/%m/%d",  "%Y-%m-%d"), ("%m-%d-%Y",   "%Y-%m-%d"),
    ]
    for fmt, out_fmt in formats:
        try:
            return datetime.strptime(raw, fmt).strftime(out_fmt)
        except ValueError:
            continue
    year_match = re.search(r'\b(1[5-9]\d{2}|20[0-2]\d)\b', raw)
    if year_match:
        return year_match.group(1)
    return None


def iso_date_to_natural(iso_date):
    if not iso_date:
        return 'n.d.'
    try:
        if re.match(r'^\d{4}-\d{2}-\d{2}$', iso_date):
            dt = datetime.strptime(iso_date, '%Y-%m-%d')
            return dt.strftime(f'%B {dt.day}, %Y')
        if re.match(r'^\d{4}-\d{2}$', iso_date):
            return datetime.strptime(iso_date, '%Y-%m').strftime('%B %Y')
        if re.match(r'^\d{4}$', iso_date):
            return iso_date
    except ValueError:
        pass
    return iso_date


def normalize_name(raw_name):
    if not raw_name or str(raw_name).strip().lower() in ('null', 'none', ''):
        return None
    name = str(raw_name).strip()
    if JUNK_NAME_PATTERNS.match(name):
        return None
    if len(name) < 3:
        return None
    if ',' in name:
        return name
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    return name


def display_name_for_title(lcnaf_name):
    """
    Convert an LCNAF-style name to a display name for the title field.
    Strips birth/death dates and parenthetical qualifiers, then converts
    from inverted form to natural order.

    Examples:
        'Peeler, A. J. (Anderson James), 1838-1886' -> 'A. J. Peeler'
        'McInnis, Louis Lowry, 1855-1933'           -> 'Louis Lowry McInnis'
        'Whitlock, R. H.'                           -> 'R. H. Whitlock'
        'Unknown'                                   -> 'Unknown'
    """
    if not lcnaf_name:
        return 'Unknown'

    name = str(lcnaf_name).strip()

    # Remove parenthetical qualifiers: (Anderson James)
    name = re.sub(r'\s*\([^)]*\)', '', name)

    # Remove trailing birth/death dates: , 1838-1886 or , 1855-
    name = re.sub(r',\s*\d{4}\s*-\s*\d{0,4}\s*$', '', name)
    # Also handle just a trailing year: , 1880
    name = re.sub(r',\s*\d{4}\s*$', '', name)

    name = name.strip().rstrip(',')

    # Convert inverted form "Last, First Middle" to "First Middle Last"
    if ',' in name:
        parts = name.split(',', 1)
        last  = parts[0].strip()
        first = parts[1].strip() if len(parts) > 1 else ''
        if first:
            return f"{first} {last}"
        return last

    return name


def resolve_name_object(obj):
    if obj is None:
        return None, None
    if isinstance(obj, str):
        return normalize_name(obj), None
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("namePart")
        uri  = obj.get("lcnaf_uri") or obj.get("valueURI")
        return normalize_name(name), uri
    return None, None


def normalize_language(raw_lang):
    if not raw_lang or str(raw_lang).strip().lower() in ('null', 'none', ''):
        return 'eng'
    lang = str(raw_lang).strip().lower()
    iso2_to_iso3 = {
        'en': 'eng', 'fr': 'fre', 'de': 'deu', 'es': 'spa',
        'it': 'ita', 'pt': 'por', 'la': 'lat', 'nl': 'dut',
    }
    return iso2_to_iso3.get(lang, lang)


def resolve_image_key(item):
    if isinstance(item, dict):
        return item.get("imageKey", ""), item.get("pageKey", "")
    return str(item), ""


def safe_image_path(img_key):
    if not img_key:
        return img_key
    return img_key if img_key.startswith("input/") else f"input/{img_key}"


def page_sort_key(k):
    nums = re.findall(r'\d+', str(k))
    return int(nums[0]) if nums else 0


# ─── Prior reasoning as plain text ────────────────────────────────────────────

def build_reasoning_context(transcribe_thinking, metadata_thinking, reconcile_thinking):
    """
    Extract the plain .thinking text from each prior stage and format it
    as a prompt section for the judge.

    Thinking block signatures are cryptographically bound to the API invocation
    that produced them and cannot be re-injected into a different invocation.
    Passing the .thinking text as plain prompt context is the correct and only
    valid cross-invocation pattern.
    """
    parts = []

    # Transcribe — stored as {page_key: {type, thinking, signature}, ...}
    if isinstance(transcribe_thinking, dict):
        for pk in sorted(transcribe_thinking.keys(), key=page_sort_key):
            block = transcribe_thinking[pk]
            if isinstance(block, dict) and block.get('thinking'):
                parts.append(f"[Transcription – {pk}]\n{block['thinking'][:1000]}")

    # Metadata — single block {type, thinking, signature}
    if isinstance(metadata_thinking, dict) and metadata_thinking.get('thinking'):
        parts.append(f"[Metadata Extraction]\n{metadata_thinking['thinking'][:2000]}")

    # Reconcile — single block, may be absent if reconciliation was skipped
    if isinstance(reconcile_thinking, dict) and reconcile_thinking.get('thinking'):
        parts.append(f"[Reconciliation]\n{reconcile_thinking['thinking'][:2000]}")

    if not parts:
        print("[INFO] No prior reasoning text available — judge runs without stage context")
        return ""

    print(f"[OK] Built reasoning context from {len(parts)} prior stage(s)")
    return (
        "\n\nPRIOR STAGE REASONING (for reference — "
        "use to understand confidence levels in the metadata):\n"
        + "\n\n".join(parts)
    )


# ─── Build structured MODS record dict ────────────────────────────────────────

def build_mods_record(raw, letter_id, image_keys):
    date     = normalize_date(raw.get('date'))
    place    = force_string(raw.get('place'))
    language = normalize_language(raw.get('language'))
    abstract = force_string(raw.get('abstract'))
    extent   = force_string(raw.get('extent'))
    note     = force_string(raw.get('note'))

    creator_name,   creator_uri   = resolve_name_object(raw.get('creator'))
    recipient_name, recipient_uri = resolve_name_object(raw.get('recipient'))

    auto_title = (
        f"Letter to {display_name_for_title(recipient_name)} "
        f"from {display_name_for_title(creator_name)}, "
        f"{iso_date_to_natural(date)}"
    )
    raw_title = force_string(raw.get('title'))

    # Sanitize LLM-provided title: strip any birth/death dates it may have included
    if raw_title:
        # Remove patterns like ", 1838-1886" or ", 1855-1933" from title
        raw_title = re.sub(r',\s*\d{4}\s*-\s*\d{4}', '', raw_title)
        # Remove parenthetical LCNAF qualifiers from title
        raw_title = re.sub(r'\s*\([^)]*\d{4}[^)]*\)', '', raw_title)
        raw_title = raw_title.strip()

    title = raw_title or auto_title

    name_array = []
    if creator_name:
        name_array.append({
            "role":        "creator",
            "roleTermUri": MARCRELATOR_CREATOR,
            "roleTerm":    "creator",
            "namePart":    creator_name,
            "authority":   "naf" if creator_uri else None,
            "valueURI":    creator_uri,
        })
    if recipient_name:
        name_array.append({
            "role":        "addressee",
            "roleTermUri": MARCRELATOR_RECIPIENT,
            "roleTerm":    "addressee",
            "namePart":    recipient_name,
            "authority":   "naf" if recipient_uri else None,
            "valueURI":    recipient_uri,
        })

    genre_label = force_string(raw.get('genre')) or 'correspondence'
    genre_uri   = raw.get('genreUri')
    # Default to AAT correspondence URI when genre is "correspondence" and no URI was resolved
    if not genre_uri and genre_label.lower() == 'correspondence':
        genre_uri = AAT_CORRESPONDENCE_URI

    raw_subjects  = raw.get('subjects') or []
    subject_array = []
    for s in raw_subjects:
        if isinstance(s, dict):
            label = s.get("label", "").strip()
            if not label:
                continue
            subject_array.append({
                "type":      s.get("type", "topic"),
                "term":      label,
                "authority": s.get("authority", "lcsh"),
                "valueURI":  s.get("value_uri"),
            })
        elif isinstance(s, str) and s.strip():
            subject_array.append({
                "type":      "topic",
                "term":      s.strip(),
                "authority": "lcsh",
                "valueURI":  None,
            })

    page_urls = []
    if isinstance(image_keys, dict):
        for pk in sorted(image_keys.keys(), key=page_sort_key):
            img_val = safe_image_path(str(image_keys[pk]))
            page_urls.append(f"s3://{PIPELINE_BUCKET}/{img_val}")
    elif isinstance(image_keys, list):
        for item in image_keys:
            img_key, _ = resolve_image_key(item)
            img_val    = safe_image_path(img_key)
            if img_val:
                page_urls.append(f"s3://{PIPELINE_BUCKET}/{img_val}")

    return {
        "modsVersion":    "3.7",
        "xmlns":          "http://www.loc.gov/mods/v3",
        "titleInfo":      {"title": title},
        "typeOfResource": "text",
        "genre": {
            "label":     genre_label,
            "authority": "aat",
            "valueURI":  genre_uri,
        },
        "name":       name_array,
        "originInfo": {
            "dateCreated": date,
            "place": place if place and place.lower() not in ('null', 'none') else None,
        },
        "language": {
            "languageTerm": language,
            "authority":    "iso639-2b",
        },
        "physicalDescription": {
            "form":   "handwritten",
            "extent": extent,
        },
        "abstract": abstract,
        "subject":  subject_array,
        "note":     note,
        "accessCondition": "Public Domain",
        "identifier": letter_id,
        "location":   {"urls": page_urls},
        "recordInfo": {
            "recordContentSource": "cursive-letters-pipeline",
            "recordCreationDate":  datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "recordOrigin": (
                "Metadata generated by AI from handwritten letter image "
                "using LOC MODS 3.7 schema. Names validated against LCNAF. "
                "Subject headings validated against LOC Authorities API. "
                "Genre resolved against Getty AAT."
            ),
        },
    }


# ─── Generate MODS XML ────────────────────────────────────────────────────────

def build_mods_xml(mods, thinking_blocks, transcription_text=""):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<mods xmlns="http://www.loc.gov/mods/v3"',
        '      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '      xsi:schemaLocation="http://www.loc.gov/mods/v3 '
        'http://www.loc.gov/standards/mods/v3/mods-3-7.xsd"',
        '      version="3.7">',
    ]

    title = xml_escape(mods["titleInfo"]["title"] or "")
    lines.append(f'  <titleInfo><title>{title}</title></titleInfo>')

    for n in mods.get("name", []):
        name_part  = xml_escape(n["namePart"] or "")
        name_attrs = ['type="personal"']
        if n.get("authority"):
            name_attrs.append(f'authority="{n["authority"]}"')
        if n.get("valueURI"):
            name_attrs.append(f'valueURI="{n["valueURI"]}"')
        role_term     = xml_escape(n["roleTerm"])
        role_term_uri = n["roleTermUri"]
        lines.append(f'  <name {" ".join(name_attrs)}>')
        lines.append(f'    <namePart>{name_part}</namePart>')
        lines.append("    <role>")
        lines.append(
            f'      <roleTerm authority="marcrelator" '
            f'valueURI="{role_term_uri}" '
            f'type="text">{role_term}</roleTerm>'
        )
        lines.append("    </role>")
        lines.append("  </name>")

    lines.append(f'  <typeOfResource>{xml_escape(mods["typeOfResource"])}</typeOfResource>')

    g = mods["genre"]
    genre_attrs = [f'authority="{g["authority"]}"']
    if g.get("valueURI"):
        genre_attrs.append(f'valueURI="{g["valueURI"]}"')
    lines.append(f'  <genre {" ".join(genre_attrs)}>{xml_escape(g["label"])}</genre>')

    oi = mods["originInfo"]
    lines.append("  <originInfo>")
    if oi.get("dateCreated"):
        lines.append(f'    <dateCreated>{xml_escape(oi["dateCreated"])}</dateCreated>')
    if oi.get("place"):
        lines.append(f'    <place><placeTerm>{xml_escape(oi["place"])}</placeTerm></place>')
    lines.append("  </originInfo>")

    lang = mods["language"]
    lines.append("  <language>")
    lines.append(
        f'    <languageTerm type="code" authority="{lang["authority"]}">'
        f'{xml_escape(lang["languageTerm"])}</languageTerm>'
    )
    lines.append("  </language>")

    if mods.get("abstract"):
        lines.append(f'  <abstract>{xml_escape(mods["abstract"])}</abstract>')

    for s in mods.get("subject", []):
        term      = xml_escape(s["term"])
        authority = s.get("authority", "lcsh")
        if s.get("type") == "name_entity":
            lines.append(f'  <subject authority="{authority}">')
            if s.get("valueURI"):
                lines.append(
                    f'    <name valueURI="{s["valueURI"]}">'
                    f'<namePart>{term}</namePart></name>'
                )
            else:
                lines.append(f'    <name><namePart>{term}</namePart></name>')
            lines.append("  </subject>")
        else:
            topic_attrs = [f'authority="{authority}"']
            if s.get("valueURI"):
                topic_attrs.append(f'valueURI="{s["valueURI"]}"')
            lines.append(f'  <subject authority="{authority}">')
            lines.append(f'    <topic {" ".join(topic_attrs)}>{term}</topic>')
            lines.append("  </subject>")

    pd = mods["physicalDescription"]
    lines.append("  <physicalDescription>")
    lines.append(f'    <form>{xml_escape(pd["form"])}</form>')
    if pd.get("extent"):
        lines.append(f'    <extent>{xml_escape(pd["extent"])}</extent>')
    lines.append("  </physicalDescription>")

    for url in mods.get("location", {}).get("urls", []):
        lines.append(f'  <location><url>{xml_escape(url)}</url></location>')

    if mods.get("note"):
        lines.append(f'  <note>{xml_escape(mods["note"])}</note>')

    ri = mods["recordInfo"]
    lines.append("  <recordInfo>")
    lines.append(
        f'    <recordContentSource>'
        f'{xml_escape(ri["recordContentSource"])}</recordContentSource>'
    )
    lines.append(f'    <recordCreationDate>{ri["recordCreationDate"]}</recordCreationDate>')
    lines.append(f'    <recordOrigin>{xml_escape(ri["recordOrigin"])}</recordOrigin>')
    lines.append("  </recordInfo>")

    lines.append("</mods>")

    # Open reasoning comment — intentionally left unclosed for CursiveJudgeParser
    lines.append("")
    lines.append("<!--")
    lines.append("  ═══════════════════════════════════════════════")
    lines.append("  AI REASONING (extended thinking per stage)")
    lines.append("  ═══════════════════════════════════════════════")

    for stage, reasoning in thinking_blocks:
        if reasoning:
            safe = reasoning.replace("--", "- -")
            lines.append(f"  [{stage.upper()}]")
            lines.append(f"  {safe}")
            lines.append("")
        if stage.lower() == "transcription" and transcription_text:
            safe_text = str(transcription_text).replace("--", "- -")
            lines.append("  [TRANSCRIPTION TEXT]")
            lines.append(f"  {safe_text}")
            lines.append("")

    # NO closing --> — left open for CursiveJudgeParser
    return "\n".join(lines)


# ─── Judge tool schema ────────────────────────────────────────────────────────

JUDGE_TOOL = {
    "name": "submit_quality_score",
    "description": (
        "Submit separate quality scores for metadata and transcription, plus a "
        "weighted final score, for a MODS record extracted from a historical handwritten letter."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "metadataScore": {
                "type": "number",
                "description": (
                    "Metadata quality score from 0.0 to 1.0. "
                    "Start at 1.0 and apply ONLY the metadata deduction rules."
                )
            },
            "transcriptionScore": {
                "type": "number",
                "description": (
                    "Transcription accuracy score from 0.0 to 1.0. "
                    "Start at 1.0 and apply ONLY the transcription deduction rules."
                )
            },
            "finalScore": {
                "type": "number",
                "description": (
                    "Weighted final score: (metadataScore × 0.6) + (transcriptionScore × 0.4). "
                    "Round to 2 decimal places."
                )
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-sentence summary of the most significant issues found "
                    "across both dimensions."
                )
            },
            "fieldIssues": {
                "type": "array",
                "description": "List of individual metadata field-level issues found.",
                "items": {
                    "type": "object",
                    "properties": {
                        "field":    {"type": "string"},
                        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                        "message":  {"type": "string"},
                    },
                    "required": ["field", "severity", "message"],
                },
            },
            "transcriptionIssues": {
                "type": "array",
                "description": (
                    "List of inconsistencies found between the transcription text "
                    "and what is visually present in the letter image(s)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "location":        {"type": "string"},
                        "imageText":       {"type": "string"},
                        "transcribedText": {"type": "string"},
                        "severity":        {"type": "string", "enum": ["critical", "major", "minor"]},
                        "note":            {"type": "string"},
                    },
                    "required": ["location", "imageText", "transcribedText", "severity"],
                },
            },
            "transcriptionQuality": {
                "type": "string",
                "enum": ["accurate", "minor_errors", "significant_errors", "unreliable"],
                "description": (
                    "Overall transcription quality label: 'accurate', 'minor_errors', "
                    "'significant_errors', or 'unreliable'."
                ),
            },
        },
        "required": [
            "metadataScore", "transcriptionScore", "finalScore",
            "reason", "fieldIssues", "transcriptionIssues", "transcriptionQuality",
        ],
    },
}


# ─── Judge prompt ─────────────────────────────────────────────────────────────

JUDGE_PROMPT = """\
You are a strict quality control judge for historical letter metadata extraction
and transcription accuracy.

You will be given:
  1) The original scanned image(s) of the handwritten letter — all pages in order.
  2) A MODS 3.7 metadata JSON extracted from that letter.
  3) The full transcription text produced from the letter.

You must produce TWO independent scores, then compute a weighted final score:
  metadataScore      — metadata quality only      (weight: 60%)
  transcriptionScore — transcription accuracy only (weight: 40%)
  finalScore         — (metadataScore × 0.6) + (transcriptionScore × 0.4)

Each score starts at 1.0. Apply ONLY the deductions listed for that dimension.

═══════════════════════════════════════════════════
PART 1 — METADATA SCORE  (start at 1.0)
═══════════════════════════════════════════════════

Use the image(s) to VISUALLY VERIFY:
  - Are creator and recipient correctly identified?
    Scan the ENTIRE letter — signature, body text, header, address block, envelope
    notation, or anywhere the sender/recipient is clearly and explicitly named.
    They do NOT have to appear only in the closing signature or opening salutation.
  - Is the date correct and visible in the letter?
  - Is the place correct and visible in the letter?
  - Does the abstract accurately reflect what the letter says?
  - Are names spelled as they appear in the letter?
  - Does the title format read "Letter to [Recipient] from [Creator], [Date]"?

METADATA DEDUCTIONS — apply to metadataScore only:

VISUAL ACCURACY:
  - Deduct 0.4 if creator and recipient are clearly swapped.
  - Deduct 0.3 if a name value is misspelled vs. what is clearly written.
  - Deduct 0.3 if a name is clearly visible anywhere in the letter but null.
  - Deduct 0.2 if place is visible but wrong or missing.
  - Deduct 0.2 if date is visible but wrong or missing.
  - Deduct 0.2 if abstract does not reflect letter content.

TITLE FORMAT:
  - Deduct 0.1 if title does not follow "Letter to X from Y, Date" format.
  - Deduct 0.1 if title uses ISO date instead of natural month name.

AUTHORITY RESOLUTION QUALITY:
  - Deduct 0.15 if genre has no valueURI (AAT unresolved).
  - Deduct 0.1  per name where valueURI is null and name is clearly
    a known historical figure who should be in LCNAF (max 0.2).
  - Deduct 0.1  per subject with valueURI null (max 0.3).

MODS COMPLIANCE:
  - Deduct 0.2 if no subject entries at all.
  - Deduct 0.1 if names not in LCNAF inverted form (Last, First).
  - Deduct 0.1 if dateCreated not ISO 8601.
  - Deduct 0.1 if languageTerm missing or invalid ISO 639-2.

Do NOT deduct for null fields when that info does not appear anywhere in the letter.

═══════════════════════════════════════════════════
PART 2 — TRANSCRIPTION SCORE  (start at 1.0)
═══════════════════════════════════════════════════

Compare the transcription text word-by-word against the letter image(s).

CHECK FOR:
  - Words misread (e.g. "farm" transcribed as "form")
  - Names misspelled relative to what is handwritten
  - Dates or numbers transcribed incorrectly
  - Words marked [?] that are actually legible in the image
  - Words marked [illegible] that can be read with care
  - Entire lines or passages missing from the transcription
  - Words or phrases inserted that do not appear in the letter
  - Punctuation that changes the meaning of a sentence

TRANSCRIPTION DEDUCTIONS — apply to transcriptionScore only:
  - Deduct 0.05 per minor misread (wrong word, does not change meaning, max 0.15)
  - Deduct 0.1  per significant misread (name/date/number wrong, max 0.3)
  - Deduct 0.15 if a full sentence or more is missing from transcription
  - Deduct 0.2  if names of sender or recipient are misread in the transcription
  - Deduct 0.1  if more than 3 [?] markers remain on clearly legible words

Report ALL transcription inconsistencies in transcriptionIssues, even minor ones.
Set transcriptionQuality based on the overall pattern:
  - 'accurate'           → no or trivial differences
  - 'minor_errors'       → a few misreads, meaning preserved
  - 'significant_errors' → names/dates/numbers wrong
  - 'unreliable'         → major portions do not match

═══════════════════════════════════════════════════
PART 3 — FINAL SCORE
═══════════════════════════════════════════════════

  finalScore = (metadataScore × 0.6) + (transcriptionScore × 0.4)
  Round to 2 decimal places.

You MUST call the submit_quality_score tool with your assessment.
Do not respond with plain text.\
"""


# ─── Build judge payload ──────────────────────────────────────────────────────

def build_judge_payload(mods_record, image_blocks, transcription_text,
                        transcribe_thinking, metadata_thinking, reconcile_thinking):
    """
    Build the Bedrock InvokeModel payload for the judge.

    Prior stage reasoning is passed as plain text extracted from each
    stage's .thinking field. Thinking block objects (with signatures)
    are never re-injected — signatures are bound to the invocation that
    produced them and are cryptographically invalid in any other call.
    """
    reasoning_context = build_reasoning_context(
        transcribe_thinking, metadata_thinking, reconcile_thinking
    )

    judge_text = (
        f"{JUDGE_PROMPT}"
        f"{reasoning_context}\n\n"
        f"TRANSCRIPTION TEXT:\n{transcription_text or '(none)'}\n\n"
        f"MODS Metadata:\n{json.dumps(mods_record, indent=2, ensure_ascii=False)}"
    )

    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens":        MAX_TOKENS,
        "thinking": {
            "type":          "enabled",
            "budget_tokens": THINKING_BUDGET,
        },
        "tools":       [JUDGE_TOOL],
        "tool_choice": {"type": "auto"},
        "messages": [
            {
                "role": "user",
                "content": image_blocks + [{"type": "text", "text": judge_text}],
            }
        ],
    }


# ─── Load images ──────────────────────────────────────────────────────────────

def load_image_blocks(image_keys):
    if not image_keys:
        return []

    if isinstance(image_keys, dict):
        ordered = sorted(image_keys.items(), key=lambda x: page_sort_key(x[0]))
    elif isinstance(image_keys, list):
        resolved = []
        for i, item in enumerate(image_keys):
            img_key, page_key = resolve_image_key(item)
            resolved.append((page_key or f"page_{i+1}", img_key))
        ordered = resolved
    else:
        return []

    total = len(ordered)
    blocks = []
    for idx, (page_label, img_key) in enumerate(ordered, start=1):
        try:
            img_path   = safe_image_path(img_key)
            ext        = img_path.lower().split(".")[-1]
            media_type = MEDIA_TYPE_MAP.get(ext, "image/jpeg")
            obj        = s3.get_object(Bucket=PIPELINE_BUCKET, Key=img_path)
            b64        = base64.b64encode(obj["Body"].read()).decode("utf-8")
            blocks.append({
                "type": "text",
                "text": f"═══ PAGE {idx} OF {total} ({page_label}) ═══"
            })
            blocks.append({
                "type": "image",
                "source": {
                    "type":       "base64",
                    "media_type": media_type,
                    "data":       b64,
                },
            })
            print(f"[OK] Loaded judge image for {page_label}: {img_path}")
        except Exception as e:
            print(f"[WARN] Could not load image for {page_label} ({img_key}): {e}")

    return blocks


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    letter_id                   = force_string(event.get('letterId'))
    raw_metadata                = event.get('rawMetadata', {})
    image_keys                  = event.get('imageKeys', {})
    judge_input_s3uri           = force_string(event.get('judgeInputS3Uri'))
    judge_output_s3uri          = force_string(event.get('judgeOutputS3Uri'))
    transcription_output_s3uri  = force_string(event.get('transcriptionOutputS3Uri'))
    transcription_by_page_s3uri = force_string(event.get('transcriptionByPageS3Uri'))
    transcribe_thinking_s3uri   = force_string(event.get('transcribeThinkingS3Uri'))
    metadata_thinking_s3uri     = force_string(event.get('metadataThinkingS3Uri'))
    reconcile_thinking_s3uri    = force_string(event.get('reconcileThinkingS3Uri'))

    if isinstance(raw_metadata, list):
        raw_metadata = raw_metadata[0] if raw_metadata else {}

    if not raw_metadata or not letter_id:
        raise ValueError(
            f"Missing required input. letterId={letter_id}, "
            f"rawMetadata present={bool(raw_metadata)}"
        )

    lid = safe_id(letter_id)

    genre_uri_override = force_string(event.get('genreUri'))
    if genre_uri_override and not raw_metadata.get('genreUri'):
        raw_metadata['genreUri'] = genre_uri_override

    enriched_subjects = event.get('enrichedSubjects', [])
    if enriched_subjects and isinstance(enriched_subjects, list):
        raw_metadata['subjects'] = enriched_subjects

    # Read large data from S3
    transcription_text  = read_s3_text(transcription_output_s3uri) or ""
    transcribe_thinking = read_s3_json(transcribe_thinking_s3uri) or {}
    metadata_thinking   = read_s3_json_optional(metadata_thinking_s3uri)
    reconcile_thinking  = read_s3_json_optional(reconcile_thinking_s3uri)

    transcriptions_dict = read_s3_json(transcription_by_page_s3uri) or {}
    page_count          = len(transcriptions_dict) if transcriptions_dict else 1

    # 1. Build structured MODS record
    mods_record = build_mods_record(raw_metadata, letter_id, image_keys)

    # 2. Build MODS XML — thinking text in comments, signatures excluded
    thinking_blocks_for_xml = [
        ("transcription",  ""),
        ("metadata",       (metadata_thinking  or {}).get('thinking', '')),
        ("reconciliation", (reconcile_thinking or {}).get('thinking', '')),
    ]
    mods_xml = build_mods_xml(
        mods_record,
        thinking_blocks_for_xml,
        transcription_text=transcription_text,
    )

    # 3. Save MODS JSON + XML to S3
    enriched_mods_key = f"intermediate/judge-input/{lid}_mods_enriched.json"
    mods_xml_key      = f"intermediate/judge-input/{lid}_mods.xml"

    _s3_put_json(enriched_mods_key, mods_record, indent=2)
    _s3_put(mods_xml_key, mods_xml, content_type="application/xml")

    # 4. Load page images for judge
    image_blocks = load_image_blocks(image_keys)
    if not image_blocks:
        print(f"[WARN] No images loaded for judge ({letter_id}) — metadata-only assessment")

    # 5. Build judge payload — prior reasoning injected as plain text only
    judge_payload = build_judge_payload(
        mods_record, image_blocks, transcription_text,
        transcribe_thinking, metadata_thinking, reconcile_thinking,
    )
    _, judge_payload_key = parse_s3_uri(judge_input_s3uri)
    _s3_put(judge_payload_key, json.dumps(judge_payload, ensure_ascii=False))

    print(
        f"[OK] MetadataFormatter for {letter_id}: "
        f"pages={page_count}, "
        f"subjects={len(mods_record.get('subject', []))}, "
        f"genre_uri={mods_record['genre'].get('valueURI') is not None}, "
        f"judge_images={len(image_blocks)}, "
        f"transcription_chars={len(transcription_text)}, "
        f"reasoning_stages="
        f"{sum([bool(transcribe_thinking), bool(metadata_thinking), bool(reconcile_thinking)])}"
    )

    return {
        "metadataRecord":           mods_record,
        "metadataS3Uri":            f"s3://{PIPELINE_BUCKET}/{enriched_mods_key}",
        "modsXmlS3Uri":             f"s3://{PIPELINE_BUCKET}/{mods_xml_key}",
        "transcriptionOutputS3Uri": transcription_output_s3uri,
        "judgeInputS3Uri":          judge_input_s3uri,
        "judgeOutputS3Uri":         judge_output_s3uri,
        "letterId":                 letter_id,
    }