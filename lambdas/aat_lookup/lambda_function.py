import boto3
import json
import os
import re
import urllib.request
import urllib.parse


s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"


# ─── Hardcoded fallback map ────────────────────────────────────────────────────

AAT_FALLBACK_MAP = {
    "correspondence":           "http://vocab.getty.edu/page/aat/300026877",
    "personal correspondence":  "http://vocab.getty.edu/page/aat/300026877",
    "letters":                  "http://vocab.getty.edu/page/aat/300026879",
    "letters (correspondence)": "http://vocab.getty.edu/page/aat/300026879",
    "circular letters":         "http://vocab.getty.edu/page/aat/300026882",
    "postcards":                "http://vocab.getty.edu/page/aat/300026816",
}

AAT_SPARQL_URL = "https://vocab.getty.edu/sparql.json"
SPARQL_TIMEOUT = 8


# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalize_label(label):
    if not label:
        return ""
    return re.sub(r'\s+', ' ', label.strip().lower())


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


# ─── AAT resolution ───────────────────────────────────────────────────────────

def resolve_via_fallback(label):
    return AAT_FALLBACK_MAP.get(normalize_label(label))


def resolve_via_sparql(label):
    normalized = normalize_label(label)

    query = f"""
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
    PREFIX gvp:  <http://vocab.getty.edu/ontology#>

    SELECT ?subject WHERE {{
      ?subject a                 gvp:Concept ;
               skos:prefLabel    ?label .
      FILTER(LCASE(STR(?label)) = "{normalized}"@en
          || LCASE(STR(?label)) = "{normalized}")
    }}
    LIMIT 1
    """

    params = urllib.parse.urlencode({"query": query, "format": "json"})
    url    = f"{AAT_SPARQL_URL}?{params}"

    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/sparql-results+json"}
        )
        with urllib.request.urlopen(req, timeout=SPARQL_TIMEOUT) as resp:
            data     = json.loads(resp.read().decode('utf-8'))
            bindings = data.get("results", {}).get("bindings", [])
            if bindings:
                sparql_uri = bindings[0]["subject"]["value"]
                page_uri   = re.sub(
                    r'http://vocab\.getty\.edu/aat/(\d+)',
                    r'http://vocab.getty.edu/page/aat/\1',
                    sparql_uri
                )
                return page_uri
    except Exception as e:
        print(f"[WARN] AAT SPARQL query failed for '{label}': {e}")

    return None


def resolve_aat_genre(label):
    """
    Resolution order:
      1. Hardcoded fallback map  — fast, no network, confirmed URIs only
      2. Getty AAT SPARQL query  — authoritative, network required
      3. None                    — unresolved; formatter will omit valueURI
    """
    if not label:
        return None, "missing"

    uri = resolve_via_fallback(label)
    if uri:
        print(f"[OK] AAT resolved via fallback map: '{label}' → {uri}")
        return uri, "fallback"

    uri = resolve_via_sparql(label)
    if uri:
        print(f"[OK] AAT resolved via SPARQL: '{label}' → {uri}")
        return uri, "sparql"

    print(f"[WARN] AAT resolution failed for genre label: '{label}'")
    return None, "unresolved"


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    raw_metadata                  = event.get('rawMetadata', {})
    letter_id                     = force_string(event.get('letterId', ''))
    transcription_by_page_s3uri   = force_string(event.get('transcriptionByPageS3Uri'))
    transcribe_thinking_s3uri     = force_string(event.get('transcribeThinkingS3Uri'))
    metadata_thinking_s3uri       = force_string(event.get('metadataThinkingS3Uri'))
    transcription_output_s3uri    = force_string(event.get('transcriptionOutputS3Uri'))
    word_positions_s3uri          = force_string(event.get('wordPositionsS3Uri'))
    judge_input_s3uri             = force_string(event.get('judgeInputS3Uri'))
    judge_output_s3uri            = force_string(event.get('judgeOutputS3Uri'))
    reconcile_thinking_s3uri      = force_string(event.get('reconcileThinkingS3Uri'))

    if isinstance(raw_metadata, list):
        raw_metadata = raw_metadata[0] if raw_metadata else {}

    genre_label = raw_metadata.get('genre', '')
    lid         = safe_id(letter_id)

    # 1. Resolve genre label to AAT URI
    genre_uri, resolution_source = resolve_aat_genre(genre_label)

    # 2. Inject resolved URI and source into rawMetadata
    raw_metadata['genreUri']              = genre_uri
    raw_metadata['genreResolutionSource'] = resolution_source

    # 3. Save resolution result to S3 for audit
    aat_result_key = f"intermediate/metadata-output/{lid}_aat_resolved.json"
    s3.put_object(
        Bucket=PIPELINE_BUCKET,
        Key=aat_result_key,
        Body=json.dumps({
            "letterId":              letter_id,
            "genreLabel":            genre_label,
            "genreUri":              genre_uri,
            "genreResolutionSource": resolution_source
        }, indent=2),
        ContentType="application/json"
    )

    print(
        f"[OK] AATLookup for {letter_id}: "
        f"label='{genre_label}', "
        f"uri={genre_uri}, "
        f"source={resolution_source}"
    )

    return {
        "letterId":                    letter_id,
        "rawMetadata":                 raw_metadata,
        "genreUri":                    genre_uri,
        "transcriptionByPageS3Uri":    transcription_by_page_s3uri,
        "transcribeThinkingS3Uri":     transcribe_thinking_s3uri,
        "metadataThinkingS3Uri":       metadata_thinking_s3uri,
        "reconcileThinkingS3Uri":      reconcile_thinking_s3uri,
        "transcriptionOutputS3Uri":    transcription_output_s3uri,
        "wordPositionsS3Uri":          word_positions_s3uri,
        "judgeInputS3Uri":             judge_input_s3uri,
        "judgeOutputS3Uri":            judge_output_s3uri,
        "aatResolved":                 genre_uri is not None,
        "aatResolutionSource":         resolution_source,
        "intermediateBucket":          PIPELINE_BUCKET
    }