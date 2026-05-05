import boto3
import json
import re
import urllib.request
import urllib.parse
from xml.etree import ElementTree as etree


s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"

BANNED_SUBJECTS = {
    "letters", "correspondence", "manuscripts",
    "documents", "history", "writing",
    "hospitality", "school field trips"
}

LOC_TIMEOUT = 10

SCHEME_LCSH  = "http://id.loc.gov/authorities/subjects"
SCHEME_LCNAF = "http://id.loc.gov/authorities/names"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def force_string(value):
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return str(value)


def extract_name_from_field(field_value):
    if field_value is None:
        return None
    if isinstance(field_value, dict):
        return (field_value.get("name") or "").strip() or None
    if isinstance(field_value, str):
        return field_value.strip() or None
    return None


def extract_label_from_subject(subject):
    if isinstance(subject, dict):
        return str(subject.get("label") or subject.get("term") or "").strip()
    if isinstance(subject, str):
        return subject.strip()
    return str(subject).strip()


def normalize_lcnaf_uri(uri):
    if uri and isinstance(uri, str):
        return uri.replace(
            "http://id.loc.gov/authorities/names/",
            "https://id.loc.gov/authorities/names/"
        )
    return uri


# ─── LOC search strategies ────────────────────────────────────────────────────

def search_suggest_exact(term, authority):
    try:
        url = (
            f"http://id.loc.gov/authorities/{authority}/suggest/"
            f"?q={urllib.parse.quote(term)}"
        )
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=LOC_TIMEOUT) as r:
            result = json.loads(r.read().decode("utf-8"))
        labels = result[1] if len(result) > 1 else []
        uris   = result[3] if len(result) > 3 else []
        term_lower = term.strip().lower()
        for i, label in enumerate(labels):
            if label.strip().lower() == term_lower and i < len(uris):
                return label.strip(), uris[i]
    except Exception as e:
        print(f"[WARN] LOC suggest failed for '{term}' ({authority}): {e}")
    return None


def search_alabel_exact(term, scheme):
    try:
        params = urllib.parse.urlencode([
            ("q", f"scheme:{scheme}"),
            ("q", f'aLabel:"{term}"'),
            ("format", "atom")
        ])
        url = f"http://id.loc.gov/search/?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/atom+xml"})
        with urllib.request.urlopen(req, timeout=LOC_TIMEOUT) as r:
            content = r.read()
        tree    = etree.fromstring(content)
        ns      = {"atom": "http://www.w3.org/2005/Atom"}
        entries = tree.findall("atom:entry", ns)
        if entries:
            entry = entries[0]
            title = entry.findtext("atom:title", default="", namespaces=ns).strip()
            link  = entry.find("atom:link[@rel='alternate']", ns)
            uri   = link.attrib.get("href", "") if link is not None else ""
            if uri:
                return title or term, uri
    except Exception as e:
        print(f"[WARN] LOC aLabel search failed for '{term}' (scheme={scheme}): {e}")
    return None


def loc_exact_match(term, authority):
    result = search_suggest_exact(term, authority)
    if result:
        return result
    scheme = SCHEME_LCNAF if authority == "names" else SCHEME_LCSH
    return search_alabel_exact(term, scheme)


# ─── LCNAF name lookup ────────────────────────────────────────────────────────

def lookup_lcnaf(name):
    if not name or not name.strip():
        return None, None
    result = loc_exact_match(name.strip(), "names")
    if result:
        label, uri = result
        uri = normalize_lcnaf_uri(uri)
        print(f"[LCNAF] Exact match: '{name}' → '{label}' ({uri})")
        return label, uri
    print(f"[LCNAF] No exact match for '{name}' — URI unresolved")
    return name.strip(), None


def resolve_names(raw_metadata, letter_id):
    for field in ("creator", "recipient"):
        field_value = raw_metadata.get(field)
        name        = extract_name_from_field(field_value)
        if not name:
            continue
        print(f"[LCNAF] Looking up {field} for {letter_id}: '{name}'")
        confirmed_name, lcnaf_uri = lookup_lcnaf(name)
        if isinstance(field_value, dict):
            if lcnaf_uri:
                raw_metadata[field]["lcnaf_uri"] = lcnaf_uri
            if confirmed_name and confirmed_name.lower() != name.lower():
                raw_metadata[field]["name"] = confirmed_name
        else:
            raw_metadata[field] = {
                "name":      confirmed_name or name,
                "lcnaf_uri": lcnaf_uri
            }
    return raw_metadata


# ─── Subject validation ───────────────────────────────────────────────────────

def validate_and_enrich_subject(subject_obj, letter_id):
    label = extract_label_from_subject(subject_obj)
    if not label:
        return None
    parts        = label.split("--")
    lookup_label = parts[0].strip()
    subdivisions = "--".join(parts[1:])

    result = loc_exact_match(lookup_label, "subjects")
    if result:
        confirmed_label, uri = result
        full_label = f"{confirmed_label}--{subdivisions}" if subdivisions else confirmed_label
        print(f"[LCSH] Matched: '{label}' → '{full_label}' ({uri})")
        return {
            "type":      "topic",
            "authority": "lcsh",
            "label":     full_label,
            "value_uri": uri
        }

    result = loc_exact_match(lookup_label, "names")
    if result:
        confirmed_label, uri = result
        uri        = normalize_lcnaf_uri(uri)
        full_label = f"{confirmed_label}--{subdivisions}" if subdivisions else confirmed_label
        print(f"[LCNAF] Matched subject: '{label}' → '{full_label}' ({uri})")
        return {
            "type":      "name_entity",
            "authority": "lcnaf",
            "label":     full_label,
            "value_uri": uri
        }

    print(f"[DROP] No exact match for '{label}' in LCSH or LCNAF — dropped ({letter_id})")
    return None


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    letter_id                     = force_string(event.get('letterId'))
    raw_metadata                  = event.get('rawMetadata', {})
    transcription_by_page_s3uri   = force_string(event.get('transcriptionByPageS3Uri'))
    transcribe_thinking_s3uri     = force_string(event.get('transcribeThinkingS3Uri'))
    metadata_thinking_s3uri       = force_string(event.get('metadataThinkingS3Uri'))
    reconcile_thinking_s3uri      = force_string(event.get('reconcileThinkingS3Uri'))
    transcription_output_s3uri    = force_string(event.get('transcriptionOutputS3Uri'))
    word_positions_s3uri          = force_string(event.get('wordPositionsS3Uri'))
    judge_input_s3uri             = force_string(event.get('judgeInputS3Uri'))
    judge_output_s3uri            = force_string(event.get('judgeOutputS3Uri'))

    if isinstance(raw_metadata, list):
        raw_metadata = raw_metadata[0] if raw_metadata else {}

    if not letter_id or not raw_metadata:
        raise ValueError(
            f"Missing required input. letterId={letter_id}, "
            f"rawMetadata present={bool(raw_metadata)}"
        )

    # 1. LCNAF lookup — updates creator/recipient in place
    raw_metadata = resolve_names(raw_metadata, letter_id)

    # 2. Extract subjects
    raw_subjects = raw_metadata.get("subjects", [])
    if not isinstance(raw_subjects, list):
        raw_subjects = []

    # 3. Filter banned labels
    filtered = []
    for s in raw_subjects:
        label = extract_label_from_subject(s)
        if label.lower() in BANNED_SUBJECTS:
            print(f"[WARN] Banned subject removed for {letter_id}: '{label}'")
            continue
        filtered.append(s)

    # 4. Validate — LCSH first, then LCNAF, exact match only, drop if no match
    enriched_subjects = []
    for subject in filtered:
        enriched = validate_and_enrich_subject(subject, letter_id)
        if enriched is not None:
            enriched_subjects.append(enriched)

    # 5. Write enriched subjects back
    raw_metadata["subjects"] = enriched_subjects

    # 6. Log summary
    lcsh_count    = sum(1 for s in enriched_subjects if s.get("authority") == "lcsh")
    lcnaf_count   = sum(1 for s in enriched_subjects if s.get("authority") == "lcnaf")
    dropped_count = len(filtered) - len(enriched_subjects)
    creator_uri   = (raw_metadata.get("creator")   or {}).get("lcnaf_uri") if isinstance(raw_metadata.get("creator"),   dict) else None
    recipient_uri = (raw_metadata.get("recipient") or {}).get("lcnaf_uri") if isinstance(raw_metadata.get("recipient"), dict) else None

    print(
        f"[OK] SubjectValidator for {letter_id}: "
        f"subjects=lcsh:{lcsh_count}/lcnaf:{lcnaf_count}/dropped:{dropped_count}, "
        f"creator_uri={'yes' if creator_uri else 'no'}, "
        f"recipient_uri={'yes' if recipient_uri else 'no'}"
    )

    return {
        "letterId":                   letter_id,
        "rawMetadata":                raw_metadata,
        "enrichedSubjects":           enriched_subjects,
        "transcriptionByPageS3Uri":   transcription_by_page_s3uri,
        "transcribeThinkingS3Uri":    transcribe_thinking_s3uri,
        "metadataThinkingS3Uri":      metadata_thinking_s3uri,
        "reconcileThinkingS3Uri":     reconcile_thinking_s3uri,
        "transcriptionOutputS3Uri":   transcription_output_s3uri,
        "wordPositionsS3Uri":         word_positions_s3uri,
        "judgeInputS3Uri":            judge_input_s3uri,
        "judgeOutputS3Uri":           judge_output_s3uri,
        "intermediateBucket":         PIPELINE_BUCKET
    }