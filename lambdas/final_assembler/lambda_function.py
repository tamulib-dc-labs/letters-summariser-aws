import boto3
import json
import re
from datetime import datetime, timezone

s3 = boto3.client('s3', region_name='us-east-2')

PIPELINE_BUCKET = "cursive-letters-pipeline"

SCORE_HIGH   = 0.85
SCORE_REVIEW = 0.65


# ─── Helpers ──────────────────────────────────────────────────────────────────

def safe_id(value):
    return re.sub(r'[^a-zA-Z0-9\-]', '_', str(value))


def force_string(value):
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return str(value)


def parse_s3_uri(uri):
    if isinstance(uri, list):
        uri = uri[0] if uri else None
    if not uri:
        return None, None
    uri = str(uri).strip()
    if not uri.startswith('s3://'):
        return None, None
    parts = uri[5:].split('/', 1)
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def read_json(uri, label=""):
    bucket, key = parse_s3_uri(uri)
    if not bucket:
        return None
    try:
        return json.loads(
            s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8')
        )
    except Exception as e:
        print(f"[WARN] Could not read JSON {label or uri}: {e}")
        return None


def read_text(uri, label=""):
    bucket, key = parse_s3_uri(uri)
    if not bucket:
        return None
    try:
        return s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8')
    except Exception as e:
        print(f"[WARN] Could not read text {label or uri}: {e}")
        return None


def _s3_put(key, body, content_type="application/json"):
    """Write body (str | bytes) to PIPELINE_BUCKET/key."""
    if isinstance(body, str):
        body = body.encode('utf-8')
    s3.put_object(Bucket=PIPELINE_BUCKET, Key=key, Body=body, ContentType=content_type)
    print(f"[OK] → s3://{PIPELINE_BUCKET}/{key}")


def score_to_status(final_score):
    """Gate on finalScore — the weighted composite of metadata + transcription."""
    if final_score >= SCORE_HIGH:
        return "accepted"
    if final_score >= SCORE_REVIEW:
        return "review_recommended"
    return "flagged"


def extract_thinking_text(thinking_data, label=""):
    """
    Return only the human-readable .thinking text from a block or paged dict.
    Strips the signature entirely — it's not useful for human review and
    is only valid within the invocation that produced it.

    Handles two shapes:
      - Single block:  {type, thinking, signature}
      - Paged dict:    {page_1: {type, thinking, signature}, page_2: ...}
    """
    if thinking_data is None:
        return None

    # Single block
    if isinstance(thinking_data, dict) and 'thinking' in thinking_data:
        return thinking_data.get('thinking')

    # Paged dict — flatten to ordered list of {page, text} for readability
    if isinstance(thinking_data, dict):
        def page_sort_key(k):
            nums = re.findall(r'\d+', str(k))
            return int(nums[0]) if nums else 0

        pages = []
        for pk in sorted(thinking_data.keys(), key=page_sort_key):
            block = thinking_data[pk]
            if isinstance(block, dict) and block.get('thinking'):
                pages.append({"page": pk, "reasoning": block['thinking']})
        return pages if pages else None

    return None


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    letter_id = force_string(event.get('letterId'))
    if not letter_id:
        raise ValueError("letterId is required")

    lid = safe_id(letter_id)

    # ── Split quality scores ──────────────────────────────────────────────────
    metadata_score      = max(0.0, min(1.0, float(event.get('metadataScore',     0.0))))
    transcription_score = max(0.0, min(1.0, float(event.get('transcriptionScore', 0.0))))
    # Recompute finalScore server-side — guards against any upstream drift
    final_score         = round(metadata_score * 0.6 + transcription_score * 0.4, 2)

    reason                = force_string(event.get('reason', ''))
    field_issues          = event.get('fieldIssues', [])
    transcription_issues  = event.get('transcriptionIssues', [])
    transcription_quality = force_string(event.get('transcriptionQuality', 'accurate'))
    status                = score_to_status(final_score)

    if not isinstance(field_issues, list):
        field_issues = []
    if not isinstance(transcription_issues, list):
        transcription_issues = []

    # ── S3 URIs ───────────────────────────────────────────────────────────────
    mods_xml_s3uri              = force_string(event.get('modsXmlS3Uri'))
    transcription_output_s3uri  = force_string(event.get('transcriptionOutputS3Uri'))
    transcription_by_page_s3uri = force_string(event.get('transcriptionByPageS3Uri'))
    word_positions_s3uri        = force_string(event.get('wordPositionsS3Uri'))
    metadata_s3_uri             = force_string(event.get('metadataS3Uri'))
    transcribe_thinking_s3uri   = force_string(event.get('transcribeThinkingS3Uri'))
    metadata_thinking_s3uri     = force_string(event.get('metadataThinkingS3Uri'))
    reconcile_thinking_s3uri    = force_string(event.get('reconcileThinkingS3Uri'))
    judge_thinking_s3uri        = force_string(event.get('judgeThinkingS3Uri'))

    # ── metadataRecord passed inline ──────────────────────────────────────────
    metadata_record = event.get('metadataRecord', {})
    if isinstance(metadata_record, list):
        metadata_record = metadata_record[0] if metadata_record else {}

    # ── Load thinking data from S3 ────────────────────────────────────────────
    transcribe_thinking_raw = read_json(transcribe_thinking_s3uri, "transcribeThinking")
    metadata_thinking_raw   = read_json(metadata_thinking_s3uri,   "metadataThinking")
    reconcile_thinking_raw  = read_json(reconcile_thinking_s3uri,  "reconcileThinking")
    judge_thinking_raw      = read_json(judge_thinking_s3uri,      "judgeThinking")

    # ── Build the bundle ──────────────────────────────────────────────────────
    bundle = {
        "letterId":    letter_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(),

        # ── Quality ──────────────────────────────────────────────────────────
        "quality": {
            # Split scores
            "metadataScore":      round(metadata_score,      4),
            "transcriptionScore": round(transcription_score, 4),
            "finalScore":         round(final_score,         4),
            # Status is driven by finalScore
            "status":               status,
            "reason":               reason,
            "transcriptionQuality": transcription_quality,
            "fieldIssues":          field_issues,
            "transcriptionIssues":  transcription_issues,
        },

        # ── Metadata ─────────────────────────────────────────────────────────
        "metadata": metadata_record or read_json(metadata_s3_uri, "metadata"),

        # ── Transcription ─────────────────────────────────────────────────────
        "transcription": {
            "combined":      read_text(transcription_output_s3uri,  "transcription"),
            "byPage":        read_json(transcription_by_page_s3uri, "transcriptionByPage"),
            "wordPositions": read_json(word_positions_s3uri,        "wordPositions"),
        },

        # ── Thinking — human-readable text only, signatures stripped ──────────
        # Signatures are cryptographically bound to their originating invocation
        # and have no value for human review. Only the reasoning text is kept.
        "thinking": {
            "transcribe": extract_thinking_text(transcribe_thinking_raw, "transcribe"),
            "metadata":   extract_thinking_text(metadata_thinking_raw,   "metadata"),
            "reconcile":  extract_thinking_text(reconcile_thinking_raw,  "reconcile"),
            "judge":      extract_thinking_text(judge_thinking_raw,      "judge"),
        },
    }

    # ── Output 1: pretty-printed JSON bundle ──────────────────────────────────
    json_key  = f"output/{lid}/{lid}_final.json"
    json_body = json.dumps(bundle, indent=2, ensure_ascii=False)
    _s3_put(json_key, json_body)
    json_uri  = f"s3://{PIPELINE_BUCKET}/{json_key}"
    print(f"[OK] Final JSON bundle → {json_key} ({len(json_body):,} chars)")

    # ── Output 2: MODS XML ────────────────────────────────────────────────────
    # Finalized by CursiveJudgeParser — copy from intermediate/ to output/
    xml_key = f"output/{lid}/{lid}_mods.xml"
    xml_uri = None
    src_bucket, src_key = parse_s3_uri(mods_xml_s3uri)
    if src_bucket and src_key:
        try:
            s3.copy_object(
                CopySource={"Bucket": src_bucket, "Key": src_key},
                Bucket=PIPELINE_BUCKET,
                Key=xml_key,
                ContentType="application/xml",
                MetadataDirective="REPLACE",
            )
            xml_uri = f"s3://{PIPELINE_BUCKET}/{xml_key}"
            print(f"[OK] Final MODS XML → {xml_key}")
        except Exception as e:
            print(f"[WARN] Could not copy MODS XML to output: {e}")

    # ── Output 3: Mirror input images into output/<lid>/images/ ───────────────
    # Makes output/<lid>/ a self-contained bundle that mirrors what GitHubSync
    # publishes to the metadata repo.
    images_copied = 0
    src_prefix = f"input/{lid}/"
    dst_prefix = f"output/{lid}/images/"
    paginator  = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=PIPELINE_BUCKET, Prefix=src_prefix):
        for obj in page.get("Contents", []):
            src = obj["Key"]
            if src.endswith("/"):
                continue
            dst = dst_prefix + src[len(src_prefix):]
            try:
                s3.copy_object(
                    CopySource={"Bucket": PIPELINE_BUCKET, "Key": src},
                    Bucket=PIPELINE_BUCKET,
                    Key=dst,
                    MetadataDirective="COPY",
                )
                images_copied += 1
            except Exception as e:
                print(f"[WARN] Could not mirror image {src} → {dst}: {e}")
    print(f"[OK] Mirrored {images_copied} input image(s) → {dst_prefix}")

    print(
        f"[OK] FinalAssembler complete for {letter_id}: "
        f"status={status}, "
        f"metadata_score={metadata_score:.2f}, "
        f"transcription_score={transcription_score:.2f}, "
        f"final_score={final_score:.2f}, "
        f"field_issues={len(field_issues)}, "
        f"transcription_issues={len(transcription_issues)}"
    )

    return {
        "letterId":             letter_id,
        "metadataScore":        round(metadata_score,      4),
        "transcriptionScore":   round(transcription_score, 4),
        "finalScore":           round(final_score,         4),
        "qualityStatus":        status,
        "transcriptionQuality": transcription_quality,
        "finalJsonS3Uri":       json_uri,
        "finalXmlS3Uri":        xml_uri,
        "intermediateBucket":   PIPELINE_BUCKET,
    }