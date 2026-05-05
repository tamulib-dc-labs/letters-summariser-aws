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


def extract_json_from_text(text):
    text = re.sub(r'```(?:json)?\s*', '', text).strip()
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise ValueError("No valid JSON object found in judge response")
    return json.loads(match.group())


def _s3_put(key, body, content_type="application/json"):
    """Write body (str | bytes) to PIPELINE_BUCKET/key."""
    if isinstance(body, str):
        body = body.encode('utf-8')
    s3.put_object(Bucket=PIPELINE_BUCKET, Key=key, Body=body, ContentType=content_type)
    print(f"[OK] → s3://{PIPELINE_BUCKET}/{key}")


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
                print(f"[WARN] tool_use not invoked (stop_reason={stop_reason}) — falling back to text parse")
                return thinking_block, None, extract_json_from_text(text)

    raise ValueError(
        f"No tool_use or text block found. "
        f"stop_reason={stop_reason}, block_types={block_types}"
    )


# ─── Append judge reasoning and close the XML comment ─────────────────────────

def finalize_mods_xml(mods_xml, judge_thinking_text,
                      metadata_score, transcription_score, final_score,
                      transcription_quality, transcription_issues):
    """
    Append [JUDGE] reasoning, split scores, transcription quality summary,
    and close the open XML comment left intentionally open by CursiveMODSFormatter.

    CursiveMODSFormatter.build_mods_xml opens the comment with:
        <!--
          AI REASONING (extended thinking per stage)
          ...prior stage reasoning...
    but deliberately does NOT write the closing -->.
    This function appends the judge section and writes the closing -->.
    """
    lines = []

    if judge_thinking_text and judge_thinking_text.strip():
        safe = judge_thinking_text.replace("--", "- -")
        lines.append("  [JUDGE]")
        lines.append(f"  {safe}")
        lines.append("")

    # Score summary block
    lines.append("  [SCORES]")
    lines.append(f"  Metadata Score:      {metadata_score:.2f}")
    lines.append(f"  Transcription Score: {transcription_score:.2f}")
    lines.append(f"  Final Score:         {final_score:.2f}  (metadata×0.6 + transcription×0.4)")
    lines.append("")

    if transcription_quality:
        lines.append(f"  [TRANSCRIPTION QUALITY: {transcription_quality.upper()}]")

    if transcription_issues:
        lines.append(f"  [TRANSCRIPTION ISSUES: {len(transcription_issues)} found]")
        for i, issue in enumerate(transcription_issues, 1):
            loc     = issue.get('location', '?')
            img_txt = issue.get('imageText', '?')
            got_txt = issue.get('transcribedText', '?')
            sev     = issue.get('severity', '?').upper()
            note    = issue.get('note', '')
            entry   = f'  {i}. [{sev}] {loc}: image="{img_txt}" transcribed="{got_txt}"'
            if note:
                entry += f" — {note}"
            lines.append(entry)
        lines.append("")

    # Close the XML comment that was left open by CursiveMODSFormatter
    lines.append("-->")
    return mods_xml + "\n" + "\n".join(lines)


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    judge_output_s3uri          = force_string(event.get('judgeOutputS3Uri'))
    letter_id                   = force_string(event.get('letterId'))
    metadata_s3_uri             = force_string(event.get('metadataS3Uri'))
    mods_xml_s3uri              = force_string(event.get('modsXmlS3Uri'))
    transcription_output_s3uri  = force_string(event.get('transcriptionOutputS3Uri'))
    transcription_by_page_s3uri = force_string(event.get('transcriptionByPageS3Uri'))
    transcribe_thinking_s3uri   = force_string(event.get('transcribeThinkingS3Uri'))
    metadata_thinking_s3uri     = force_string(event.get('metadataThinkingS3Uri'))
    reconcile_thinking_s3uri    = force_string(event.get('reconcileThinkingS3Uri'))
    word_positions_s3uri        = force_string(event.get('wordPositionsS3Uri'))
    judge_input_s3uri           = force_string(event.get('judgeInputS3Uri'))

    # Unwrap list → single dict if Step Functions wrapped it
    metadata_record = event.get('metadataRecord', {})
    if isinstance(metadata_record, list):
        metadata_record = metadata_record if metadata_record else {}

    if not judge_output_s3uri or not letter_id:
        raise ValueError(
            f"Missing required input. "
            f"judgeOutputS3Uri={judge_output_s3uri}, letterId={letter_id}"
        )

    lid = safe_id(letter_id)

    # 1. Read judge Bedrock output from S3
    bucket, key = parse_s3_uri(judge_output_s3uri)
    raw = json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8'))

    # 2. Extract thinking block and judge result
    thinking_block, _, judge_result = extract_thinking_and_tool_result(
        raw, expected_tool_name="submit_quality_score"
    )

    # 3. Parse split scores
    metadata_score      = float(judge_result.get('metadataScore',     0.0))
    transcription_score = float(judge_result.get('transcriptionScore', 0.0))
    final_score         = float(judge_result.get('finalScore',         0.0))
    reason              = force_string(judge_result.get('reason', ''))
    field_issues        = judge_result.get('fieldIssues', [])
    transcription_issues  = judge_result.get('transcriptionIssues', [])
    transcription_quality = force_string(judge_result.get('transcriptionQuality', 'accurate'))

    if not isinstance(field_issues, list):
        field_issues = []
    if not isinstance(transcription_issues, list):
        transcription_issues = []

    # 4. Clamp all scores to [0.0, 1.0]; recompute finalScore server-side as
    #    a safety net in case the model rounded incorrectly
    metadata_score      = max(0.0, min(1.0, metadata_score))
    transcription_score = max(0.0, min(1.0, transcription_score))
    final_score         = round(metadata_score * 0.6 + transcription_score * 0.4, 2)

    # 5. Read MODS XML from S3
    mods_xml = None
    if mods_xml_s3uri:
        try:
            mods_xml = read_s3_text(mods_xml_s3uri)
        except Exception as e:
            print(f"[WARN] Could not read modsXml from {mods_xml_s3uri}: {e}")

    # 6. Finalize MODS XML — append judge reasoning + scores and close the open comment.
    #    The thinking block signature is intentionally NOT written here — it stays
    #    only in the intermediate reasoning JSON saved in step 7.
    judge_thinking_text = thinking_block.get('thinking', '') if thinking_block else ''
    finalized_xml   = finalize_mods_xml(
        mods_xml, judge_thinking_text,
        metadata_score, transcription_score, final_score,
        transcription_quality, transcription_issues,
    ) if mods_xml else None
    final_xml_s3uri = None

    if finalized_xml:
        # Overwrite the upstream intermediate XML in-place
        _, upstream_key = parse_s3_uri(mods_xml_s3uri)
        _s3_put(upstream_key, finalized_xml, content_type="application/xml")
        final_xml_s3uri = mods_xml_s3uri

        # Audit copy under judge-output prefix
        _s3_put(
            f"intermediate/judge-output/{lid}_mods.xml",
            finalized_xml,
            content_type="application/xml",
        )

    # 7. Save judge thinking block as JSON — signature preserved in full.
    #    CursiveFinalAssembler reads this file and strips the signature
    #    before final output.
    judge_thinking_s3uri = None
    if thinking_block:
        reasoning_key = f"intermediate/reasoning/{lid}_judge_reasoning.json"
        _s3_put(reasoning_key, json.dumps(thinking_block, separators=(',', ':')))
        judge_thinking_s3uri = f"s3://{PIPELINE_BUCKET}/{reasoning_key}"
        print(f"[OK] Judge reasoning saved with signature → {reasoning_key}")

    # 8. Log metadata issues
    critical = [f for f in field_issues if f.get('severity') == 'critical']
    major    = [f for f in field_issues if f.get('severity') == 'major']
    for issue in critical + major:
        print(
            f"[ISSUE] {issue.get('severity','?').upper()} — "
            f"{issue.get('field','?')}: {issue.get('message','')}"
        )

    # 9. Log transcription issues
    t_critical = [t for t in transcription_issues if t.get('severity') == 'critical']
    t_major    = [t for t in transcription_issues if t.get('severity') == 'major']
    for issue in t_critical + t_major:
        print(
            f"[TRANSCRIPTION] {issue.get('severity','?').upper()} — "
            f"{issue.get('location','?')}: "
            f"image=\"{issue.get('imageText','?')}\" "
            f"transcribed=\"{issue.get('transcribedText','?')}\""
        )

    print(
        f"[OK] JudgeParser for {letter_id}: "
        f"metadata_score={metadata_score:.2f}, "
        f"transcription_score={transcription_score:.2f}, "
        f"final_score={final_score:.2f}, "
        f"critical={len(critical)}, major={len(major)}, "
        f"total_issues={len(field_issues)}, "
        f"transcription_quality={transcription_quality}, "
        f"transcription_issues={len(transcription_issues)}, "
        f"thinking={'yes (signature saved)' if thinking_block else 'no'}"
    )

    return {
        "metadataScore":            metadata_score,
        "transcriptionScore":       transcription_score,
        "finalScore":               final_score,
        "reason":                   reason,
        "fieldIssues":              field_issues,
        "transcriptionIssues":      transcription_issues,
        "transcriptionQuality":     transcription_quality,
        "metadataRecord":           metadata_record,
        "metadataS3Uri":            metadata_s3_uri,
        "modsXmlS3Uri":             final_xml_s3uri or mods_xml_s3uri,
        "transcriptionOutputS3Uri": transcription_output_s3uri,
        "transcriptionByPageS3Uri": transcription_by_page_s3uri,
        "transcribeThinkingS3Uri":  transcribe_thinking_s3uri,
        "metadataThinkingS3Uri":    metadata_thinking_s3uri,
        "reconcileThinkingS3Uri":   reconcile_thinking_s3uri,
        "judgeThinkingS3Uri":       judge_thinking_s3uri,
        "wordPositionsS3Uri":       word_positions_s3uri,
        "judgeInputS3Uri":          judge_input_s3uri,
        "judgeOutputS3Uri":         judge_output_s3uri,
        "letterId":                 letter_id,
        "intermediateBucket":       PIPELINE_BUCKET,
    }