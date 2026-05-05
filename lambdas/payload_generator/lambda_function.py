import base64
import io
import json
import re
import time

import boto3
from PIL import Image

s3      = boto3.client('s3',              region_name='us-east-2')
bedrock = boto3.client('bedrock-runtime', region_name='us-east-2')

PIPELINE_BUCKET      = "cursive-letters-pipeline"
SONNET_MODEL_ID      = "us.anthropic.claude-sonnet-4-6-20250514-v1:0"
OPUS_THINKING_BUDGET = 16000
OPUS_MAX_TOKENS      = 24000
SONNET_MAX_TOKENS    = 2000
LINE_PADDING         = 4
MIN_CROP_PX          = 8

# ─── Bleed-through thresholds ─────────────────────────────────────────────────
BLEED_HARD_DROP   = 25.0   # always drop — pure noise regardless of neighbors
BLEED_SOFT_DROP   = 42.0   # drop only when confirmed as cluster
BLEED_RADIUS      = 0.06   # spatial search radius (6% of image dimension)
BLEED_MIN_CLUSTER = 2      # min low-conf neighbors required to confirm cluster

MEDIA_TYPE_MAP = {
    "jpg":  "image/jpeg", "jpeg": "image/jpeg",
    "png":  "image/png",  "tiff": "image/tiff",
    "tif":  "image/tiff", "gif":  "image/gif",
    "webp": "image/webp"
}

LAYOUT_LABEL_MAP = {
    "LAYOUT_HEADER":         "HEADER / LETTERHEAD",
    "LAYOUT_TITLE":          "TITLE",
    "LAYOUT_SECTION_HEADER": "SECTION HEADING",
    "LAYOUT_TEXT":           "BODY TEXT",
    "LAYOUT_KEY_VALUE":      "KEY-VALUE FIELD",
    "LAYOUT_TABLE":          "TABLE",
    "LAYOUT_FIGURE":         "FIGURE",
    "LAYOUT_FOOTER":         "FOOTER",
    "LAYOUT_PAGE_NUMBER":    "PAGE NUMBER",
    "SIGNATURE":             "HANDWRITTEN SIGNATURE — read directly from image",
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


def img_to_b64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('utf-8')


# ─── Textract helpers ─────────────────────────────────────────────────────────

def get_words_in_reading_order(blocks: list) -> list:
    """
    Follow PAGE → LINE → WORD relationships for correct reading order.
    Falls back to spatial sort (top then left) if relationships are absent.
    """
    block_map = {b['Id']: b for b in blocks if 'Id' in b}

    page_blocks = [b for b in blocks if b.get('BlockType') == 'PAGE']
    if page_blocks:
        words    = []
        line_ids = []
        for rel in page_blocks[0].get('Relationships', []):
            if rel.get('Type') == 'CHILD':
                line_ids = rel.get('Ids', [])
                break
        for lid in line_ids:
            line = block_map.get(lid, {})
            if line.get('BlockType') != 'LINE':
                continue
            for rel in line.get('Relationships', []):
                if rel.get('Type') == 'CHILD':
                    for wid in rel.get('Ids', []):
                        w = block_map.get(wid, {})
                        if w.get('BlockType') == 'WORD' and 'Geometry' in w:
                            words.append(w)
        if words:
            return words

    # Fallback — sort spatially
    words = [b for b in blocks if b.get('BlockType') == 'WORD' and 'Geometry' in b]
    words.sort(key=lambda w: (
        round(w['Geometry']['BoundingBox']['Top'], 2),
        w['Geometry']['BoundingBox']['Left']
    ))
    return words


def build_layout_scaffold(blocks: list) -> str:
    layout_types = set(LAYOUT_LABEL_MAP.keys())
    sections = []
    for b in blocks:
        bt = b.get('BlockType', '')
        if bt not in layout_types or 'Geometry' not in b:
            continue
        box = b['Geometry']['BoundingBox']
        sections.append({
            'label':  LAYOUT_LABEL_MAP[bt],
            'type':   bt,
            'top':    box['Top'],
            'left':   box['Left'],
            'width':  box['Width'],
            'height': box['Height'],
        })

    sections.sort(key=lambda s: s['top'])

    if not sections:
        return "No layout sections detected — read the full image top to bottom."

    lines = ["DOCUMENT LAYOUT SECTIONS (reading order, top → bottom):"]
    for i, s in enumerate(sections, 1):
        lines.append(
            f"  [{i:02d}] {s['label']}"
            f"  — top={s['top']:.3f}  left={s['left']:.3f}"
            f"  w={s['width']:.3f}  h={s['height']:.3f}"
        )
    lines.append(
        "\nBounding boxes are fractions of image size. "
        "top=0.08 means 8% from the top edge. "
        "Navigate to each section bbox to read its content."
    )
    return "\n".join(lines)


# ─── Layout zone extraction ──────────────────────────────────────────────────

HEADER_COL_GAP_THRESHOLD = 0.12   # 12% page width gap → column boundary
HEADER_COL_MIN_WORDS     = 3      # minimum words to form a valid column


def classify_line_zone(line_bbox: dict, layout_zones: list) -> str:
    """
    Classify a Textract LINE bbox into 'header', 'body', 'ending', or 'other'
    using Textract's content-aware LAYOUT blocks. We trust Textract's labels —
    we do not recompute layout geometry ourselves.
    """
    cy = line_bbox['Top']  + line_bbox['Height'] / 2
    cx = line_bbox['Left'] + line_bbox['Width']  / 2
    matched = [
        z['type'] for z in layout_zones
        if z['top']  <= cy <= z['bottom']
        and z['left'] <= cx <= z['right']
    ]
    if 'LAYOUT_TEXT' in matched:
        return 'body'
    if 'LAYOUT_HEADER' in matched:
        return 'header'
    if 'LAYOUT_FOOTER' in matched or 'LAYOUT_PAGE_NUMBER' in matched:
        return 'ending'
    return 'other'


def extract_layout_zones(blocks: list) -> list:
    """
    Extract layout zone boundaries from Textract LAYOUT blocks.
    Returns a list of zone dicts: {type, top, left, width, height, bottom, right}
    sorted by vertical position.
    """
    layout_types = set(LAYOUT_LABEL_MAP.keys())
    zones = []
    for b in blocks:
        bt = b.get('BlockType', '')
        if bt not in layout_types or 'Geometry' not in b:
            continue
        box = b['Geometry']['BoundingBox']
        zones.append({
            'type':   bt,
            'top':    round(box['Top'], 4),
            'left':   round(box['Left'], 4),
            'width':  round(box['Width'], 4),
            'height': round(box['Height'], 4),
            'bottom': round(box['Top'] + box['Height'], 4),
            'right':  round(box['Left'] + box['Width'], 4),
        })
    zones.sort(key=lambda z: z['top'])
    return zones


def detect_header_columns(word_positions: list, layout_zones: list) -> list:
    """
    Detect columns within header zone(s) by clustering word X-positions.
    Uses gap analysis: finds horizontal gaps > HEADER_COL_GAP_THRESHOLD.
    
    Returns list of column dicts sorted left-to-right:
      [{left, right, word_count, approx_content}, ...]
    Returns empty list if no header zone or single-column header.
    """
    # Find header zone boundaries
    header_zones = [z for z in layout_zones if z['type'] == 'LAYOUT_HEADER']
    if not header_zones:
        return []

    # Collect words that fall within any header zone
    header_words = []
    for wp in word_positions:
        wp_cx = wp['left'] + wp['width'] / 2
        wp_cy = wp['top'] + wp['height'] / 2
        for hz in header_zones:
            if (hz['top'] <= wp_cy <= hz['bottom'] and
                hz['left'] <= wp_cx <= hz['right']):
                header_words.append(wp)
                break

    if len(header_words) < HEADER_COL_MIN_WORDS * 2:
        # Not enough words for multi-column detection
        return []

    # Sort words by left position for gap analysis
    sorted_by_x = sorted(header_words, key=lambda w: w['left'])

    # Build word X-position clusters using gap analysis
    clusters = []
    current_cluster = [sorted_by_x[0]]

    for i in range(1, len(sorted_by_x)):
        prev_right = sorted_by_x[i-1]['left'] + sorted_by_x[i-1]['width']
        curr_left  = sorted_by_x[i]['left']
        gap = curr_left - prev_right

        if gap > HEADER_COL_GAP_THRESHOLD:
            clusters.append(current_cluster)
            current_cluster = [sorted_by_x[i]]
        else:
            current_cluster.append(sorted_by_x[i])

    clusters.append(current_cluster)

    # Filter out tiny clusters (noise)
    valid_clusters = [c for c in clusters if len(c) >= HEADER_COL_MIN_WORDS]

    if len(valid_clusters) <= 1:
        return []  # Single column, no special handling needed

    # Build column descriptors
    columns = []
    for cluster in valid_clusters:
        lefts  = [w['left'] for w in cluster]
        rights = [w['left'] + w['width'] for w in cluster]
        # Get a sample of text for the prompt description
        sample_texts = [w.get('sonnet_text', w.get('textract_text', '')) for w in cluster[:3]]
        approx = ' '.join(sample_texts)
        columns.append({
            'left':           round(min(lefts), 3),
            'right':          round(max(rights), 3),
            'word_count':     len(cluster),
            'approx_content': approx[:60],
        })

    columns.sort(key=lambda c: c['left'])
    print(f"[INFO] Detected {len(columns)} header columns: " +
          ", ".join(f"x={c['left']:.2f}–{c['right']:.2f} ({c['word_count']} words)" for c in columns))
    return columns


def build_column_order_instructions(columns: list) -> str:
    """
    Build explicit column-ordering instructions for the Opus prompt
    when multi-column headers are detected.
    """
    if not columns or len(columns) < 2:
        return ""

    labels = ["LEFT", "CENTER", "RIGHT", "FAR-RIGHT"]
    lines = [
        "",
        f"  The letterhead has {len(columns)} COLUMNS detected:",
    ]
    for i, col in enumerate(columns):
        label = labels[i] if i < len(labels) else f"COLUMN {i+1}"
        lines.append(
            f"    {label} column (x={col['left']:.2f}–{col['right']:.2f}): "
            f"{col['word_count']} words, starts with \"{col['approx_content'][:30]}...\""
        )

    lines.extend([
        "",
        "  CRITICAL: Output ALL lines of the LEFT column first (top to bottom),",
        "  then ALL lines of the CENTER column, then ALL lines of the RIGHT column.",
        "  Do NOT interleave text from different columns on the same output line.",
        "  Each column forms a separate block in your transcription.",
    ])
    return "\n".join(lines)


# ─── Line + word extraction from Textract ────────────────────────────────────

def get_lines_with_words(blocks: list) -> list:
    """
    Extract LINE blocks with their child WORD blocks from Textract output.
    Returns list of dicts: {line_block, words, line_index, bbox}
    sorted in reading order (top then left).
    """
    block_map = {b['Id']: b for b in blocks if 'Id' in b}

    line_blocks = [b for b in blocks if b.get('BlockType') == 'LINE' and 'Geometry' in b]
    # Sort by top then left for reading order
    line_blocks.sort(key=lambda b: (
        round(b['Geometry']['BoundingBox']['Top'], 3),
        b['Geometry']['BoundingBox']['Left']
    ))

    result = []
    for line_idx, lb in enumerate(line_blocks):
        bbox = lb['Geometry']['BoundingBox']
        word_ids = []
        for rel in lb.get('Relationships', []):
            if rel.get('Type') == 'CHILD':
                word_ids = rel.get('Ids', [])
                break

        words = []
        for wid in word_ids:
            w = block_map.get(wid, {})
            if w.get('BlockType') == 'WORD' and 'Geometry' in w:
                words.append(w)

        if words:
            result.append({
                'line_block': lb,
                'words':      words,
                'line_index': line_idx,
                'bbox':       bbox,
            })

    return result


# ─── Cropping ────────────────────────────────────────────────────────────────

def crop_region(page_img: Image.Image, bbox: dict, img_w: int, img_h: int, padding: int):
    x1 = max(0,     int(bbox['Left']                    * img_w) - padding)
    y1 = max(0,     int(bbox['Top']                     * img_h) - padding)
    x2 = min(img_w, int((bbox['Left'] + bbox['Width'])  * img_w) + padding)
    y2 = min(img_h, int((bbox['Top']  + bbox['Height']) * img_h) + padding)

    if (x2 - x1) < MIN_CROP_PX or (y2 - y1) < MIN_CROP_PX:
        return None
    return page_img.crop((x1, y1, x2, y2))


# ─── Bleed-through detection ──────────────────────────────────────────────────

def detect_bleedthrough(word_blocks: list) -> tuple:
    """
    Identify word blocks caused by ink bleed-through from the reverse page.

    Two-pass strategy:
      Hard drop  — conf < BLEED_HARD_DROP: always exclude (pure noise)
      Soft drop  — conf < BLEED_SOFT_DROP: exclude only when the word has
                   >= BLEED_MIN_CLUSTER other low-conf neighbors within
                   BLEED_RADIUS AND no high-conf words nearby.
                   Isolated low-conf words (hard cursive) are preserved.

    Returns:
        drop_ids        : set of block IDs to skip in Haiku batching
        cluster_regions : list of {top, left, bottom, right, word_count}
                          bounding boxes of bleed clusters — injected into
                          the Opus prompt as explicit ignore-regions.
    """
    candidates = []   # (id, conf, cx, cy, bbox)  conf < BLEED_SOFT_DROP
    safe_words  = []  # (id, conf, cx, cy)         conf >= BLEED_SOFT_DROP

    for wb in word_blocks:
        conf = wb.get('Confidence', 0.0)
        bbox = wb['Geometry']['BoundingBox']
        cx   = bbox['Left'] + bbox['Width']  / 2
        cy   = bbox['Top']  + bbox['Height'] / 2

        if conf < BLEED_SOFT_DROP:
            candidates.append((wb['Id'], conf, cx, cy, bbox))
        else:
            safe_words.append((wb['Id'], conf, cx, cy))

    drop_ids     = set()
    drop_entries = []   # (cx, cy, bbox) for cluster bbox computation

    for wid, conf, cx, cy, bbox in candidates:
        # Pass 1 — hard drop
        if conf < BLEED_HARD_DROP:
            drop_ids.add(wid)
            drop_entries.append((cx, cy, bbox))
            continue

        # Pass 2 — soft drop: needs cluster confirmation
        low_conf_neighbors = sum(
            1 for nid, _, nx, ny, _ in candidates
            if nid != wid
            and abs(nx - cx) < BLEED_RADIUS
            and abs(ny - cy) < BLEED_RADIUS
        )
        safe_neighbors = sum(
            1 for _, _, nx, ny in safe_words
            if abs(nx - cx) < BLEED_RADIUS
            and abs(ny - cy) < BLEED_RADIUS
        )

        # Cluster with no high-conf anchor nearby → bleed-through
        if low_conf_neighbors >= BLEED_MIN_CLUSTER and safe_neighbors == 0:
            drop_ids.add(wid)
            drop_entries.append((cx, cy, bbox))

    cluster_regions = _compute_cluster_regions(drop_entries)

    print(
        f"[INFO] Bleed-through filter: {len(drop_ids)}/{len(word_blocks)} words dropped, "
        f"{len(cluster_regions)} cluster region(s) detected"
    )
    return drop_ids, cluster_regions


def _compute_cluster_regions(drop_entries: list) -> list:
    """
    Group dropped word centroids into spatial clusters, then return a padded
    bounding box per cluster for injection into the Opus prompt.
    """
    if not drop_entries:
        return []

    MERGE_RADIUS = BLEED_RADIUS * 2.0
    clusters = []

    for cx, cy, bbox in drop_entries:
        placed = False
        for cluster in clusters:
            if any(
                abs(cx - ocx) < MERGE_RADIUS and abs(cy - ocy) < MERGE_RADIUS
                for ocx, ocy, _ in cluster
            ):
                cluster.append((cx, cy, bbox))
                placed = True
                break
        if not placed:
            clusters.append([(cx, cy, bbox)])

    regions = []
    for cluster in clusters:
        boxes  = [b for _, _, b in cluster]
        top    = max(0.0, min(b['Top']               for b in boxes) - 0.02)
        left   = max(0.0, min(b['Left']              for b in boxes) - 0.02)
        bottom = min(1.0, max(b['Top'] + b['Height'] for b in boxes) + 0.02)
        right  = min(1.0, max(b['Left'] + b['Width'] for b in boxes) + 0.02)
        regions.append({
            'top':        round(top,    3),
            'left':       round(left,   3),
            'bottom':     round(bottom, 3),
            'right':      round(right,  3),
            'word_count': len(cluster),
        })

    return regions


def build_bleedthrough_warning(cluster_regions: list) -> str:
    """
    Render the bleed-through regions as a human-readable block for the Opus prompt.
    """
    if not cluster_regions:
        return "  No bleed-through regions detected on this page."

    lines = []
    for i, r in enumerate(cluster_regions, 1):
        lines.append(
            f"  Region {i}:  top={r['top']:.3f}  left={r['left']:.3f}  "
            f"bottom={r['bottom']:.3f}  right={r['right']:.3f}  "
            f"({r['word_count']} low-confidence ink detections)"
        )
    return "\n".join(lines)


# ─── Sonnet line-level transcription ─────────────────────────────────────────

def _parse_sonnet_line_response(text: str, word_count: int, words: list) -> list:
    """
    Parse Sonnet's JSON array response for a single line.
    Returns list of corrected texts, one per word position.
    Falls back to Textract text for any missing entries.
    """
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON array in Sonnet response: {text[:200]}")

    items = json.loads(match.group())

    result = []
    for i in range(word_count):
        if i < len(items) and isinstance(items[i], dict) and 'text' in items[i]:
            result.append(items[i]['text'])
        elif i < len(items) and isinstance(items[i], str):
            result.append(items[i])
        else:
            # Fallback to Textract
            fallback = words[i].get('Text', '[illegible]') if i < len(words) else '[illegible]'
            print(f"[WARN] Sonnet missing word {i+1}/{word_count} — Textract fallback: {fallback}")
            result.append(fallback)

    return result


def transcribe_line(line_crop: Image.Image, words: list) -> list:
    """
    Send a single line crop to Sonnet with Textract word hints.
    Returns list of corrected texts, one per Textract word position.
    """
    content = []

    # Line image
    content.append({
        "type": "image",
        "source": {
            "type":       "base64",
            "media_type": "image/png",
            "data":       img_to_b64_png(line_crop)
        }
    })

    # Word hints from Textract
    word_hints = []
    for i, wb in enumerate(words, start=1):
        t_text = wb.get('Text', '')
        conf   = wb.get('Confidence', 0.0)
        word_hints.append(f"  {i}. \"{t_text}\" (conf={conf:.0f}%)")

    content.append({
        "type": "text",
        "text": (
            f"This image is one line from a historical document.\n"
            f"OCR detected {len(words)} words in this line:\n"
            + "\n".join(word_hints) + "\n\n"
            f"Read the line from the image. The OCR readings above are hints — "
            f"if you read something different, use YOUR reading.\n"
            f"You MUST return exactly {len(words)} words to match the OCR word count.\n"
            f"If OCR split one word into two (e.g. \"to\" + \"day\" for \"today\"), "
            f"keep the same split so counts match.\n"
            f"Uncertain: append [?]. Illegible: \"[illegible]\".\n"
            f"Return ONLY a JSON array with exactly {len(words)} entries, in order:\n"
            f"[{{\"id\": 1, \"text\": \"Dear\"}}, {{\"id\": 2, \"text\": \"Prof.\"}}, ...]"
        )
    })

    for attempt in range(3):
        try:
            resp = bedrock.invoke_model(
                modelId=SONNET_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": SONNET_MAX_TOKENS,
                    "messages": [{"role": "user", "content": content}]
                })
            )
            body = json.loads(resp['body'].read())
            text = body['content'][0]['text'].strip()
            return _parse_sonnet_line_response(text, len(words), words)

        except Exception as e:
            if attempt == 2:
                print(f"[WARN] Sonnet line failed after 3 attempts: {e} — Textract fallback")
                return [wb.get('Text', '[illegible]') for wb in words]
            wait = 2 ** attempt
            print(f"[WARN] Sonnet attempt {attempt + 1} failed: {e} — retrying in {wait}s")
            time.sleep(wait)


# ─── Word draft context for Opus ──────────────────────────────────────────────

DRAFT_CONF_THRESHOLD = 75.0   # only show high-confidence words in draft

def build_word_draft_context(word_positions: list) -> str:
    """
    Build the word-level draft string sent to Opus.
    Only includes HIGH-CONFIDENCE words (>= 75% Textract confidence) to
    avoid poisoning Opus with bad cursive readings.
    Excludes dropped_bleedthrough entries entirely.
    """
    active = [
        wp for wp in word_positions
        if wp['source'] not in ('dropped_bleedthrough', 'textract_fallback')
    ]

    if not active:
        return "No word-level draft available."

    high_conf = [wp for wp in active if wp.get('textractConfidence', 0) >= DRAFT_CONF_THRESHOLD]
    low_conf  = [wp for wp in active if wp.get('textractConfidence', 0) < DRAFT_CONF_THRESHOLD]

    lines = [
        "WORD-LEVEL SPATIAL ANCHORS (high-confidence detections only):",
        "These are POSITION GUIDES to help you locate words in the image.",
        "The draft text is a rough OCR reading — it WILL contain errors.",
        "ALWAYS trust what YOU read in the IMAGE over any draft text below.",
        "",
        f"Showing {len(high_conf)} high-confidence words (of {len(active)} total detections).",
        f"{len(low_conf)} low-confidence detections omitted — read those regions directly from the image.",
        "",
        "Format: word_N | draft_hint | top | left | width | height",
        ""
    ]
    for wp in high_conf:
        lines.append(
            f"  word_{wp['wordIndex']:03d} | \"{wp['sonnet_text']}\""
            f" | top={wp['top']:.3f} left={wp['left']:.3f}"
            f" w={wp['width']:.3f} h={wp['height']:.3f}"
        )
    return "\n".join(lines)


# ─── Opus tool + prompt ───────────────────────────────────────────────────────

TRANSCRIPTION_TOOL = {
    "name": "submit_transcription",
    "description": "Submit the full verbatim transcription of one page of a historical letter.",
    "input_schema": {
        "type": "object",
        "properties": {
            "transcriptionText": {
                "type": "string",
                "description": (
                    "Full verbatim transcription of ALL text on this page — "
                    "including printed letterhead, headers, and handwritten content. "
                    "Preserve original spelling (e.g. 'Honour'd', 'ye'), punctuation, and line breaks. "
                    "Each line in the original should be a separate line in the output. "
                    "Uncertain words: best attempt + [?]  e.g. 'Thomas[?]', '1847[?]'. "
                    "Truly unreadable: [illegible]. "
                    "Your transcription must match what you determined in your thinking."
                )
            },
            "imageKey": {
                "type": "string",
                "description": "The S3 image key passed in from the pipeline."
            }
        },
        "required": ["transcriptionText", "imageKey"]
    }
}

TRANSCRIPTION_PROMPT_TEMPLATE = """\
You are an expert paleographer specialising in historical documents — \
cursive script, archaic spelling, faded ink, and 17th–20th century letter conventions.

══════════════════════════════════════════════════════
  ⚠️  CRITICAL: THE IMAGE IS YOUR ONLY REAL SOURCE.
══════════════════════════════════════════════════════

Read ALL text on this page directly from the image. This includes:
  • Printed letterhead (organisation name, staff, address, etc.)
  • Handwritten date line, salutation, body, closing, signature, P.S.

Transcribe EVERYTHING visible — both printed and handwritten text.
Each line on the page should be a separate line in your output.

══════════════════════════════════════════════════════
  MULTI-COLUMN LAYOUTS (letterheads, headers, footers)
══════════════════════════════════════════════════════

  Printed letterheads often have 2–3 COLUMNS side by side (e.g. staff
  list on left, institution name in centre, board members on right).

  READ EACH COLUMN SEPARATELY, top to bottom, before moving to the next.
  Do NOT read across columns on the same horizontal line.

  Example — a 3-column header should be transcribed as:
    [left column, all lines top to bottom]
    [blank line]
    [centre column, all lines top to bottom]
    [blank line]
    [right column, all lines top to bottom]

  Insert a blank line between each column block to clearly separate them.
{column_order_instructions}

  Similarly, archival notations, stamps, or annotations near the
  signature at the bottom of a page should be read exactly as written —
  do not merge them with the closing text above.

The supplementary data below (layout scaffold, spatial anchors) exists
ONLY to help you LOCATE regions on the page. Their TEXT CONTENT IS
UNRELIABLE — they were produced by OCR and contain frequent errors.

══════════════════════════════════════════════════════
  INK BLEED-THROUGH — IGNORE THESE REGIONS
══════════════════════════════════════════════════════

  Historical letter paper is thin. Ink from the REVERSE side bleeds through
  as faint, often mirror-image ghost text. Ignore ALL ink in these regions:

{bleedthrough_warning}

  If faint text does not fit the grammatical flow → it is bleed-through → omit it.

══════════════════════════════════════════════════════
  HOW TO HANDLE DIFFICULT WORDS
══════════════════════════════════════════════════════

  • Uncertain but readable  →  best attempt + [?]   e.g. "Thomas[?]", "1847[?]"
  • Truly unreadable        →  [illegible]
  • Partial word visible    →  visible part + [?]   e.g. "W[illia]m[?]"

  DATES — read every digit carefully. Common misreads: 0↔9, 7↔1, 8↔3.
  NAMES — read letter by letter. Proper names are the hardest to get right.

══════════════════════════════════════════════════════
  SUPPLEMENTARY SPATIAL DATA (position guides only)
══════════════════════════════════════════════════════

{layout_scaffold}

{word_draft_context}

══════════════════════════════════════════════════════
  RULES
══════════════════════════════════════════════════════

  • Transcribe ALL text: printed letterhead AND handwritten content.
  • Each visual line on the page = one line in your output.
  • Preserve original spelling exactly — do NOT modernise.
  • Preserve punctuation as written.
  • Do NOT summarise, interpret, or add commentary.
  • Do NOT add anything not visible on THIS SIDE of the page.
  • When in doubt, OMIT rather than invent.

  ⚠️  IMPORTANT: Your thinking may contain your best reading of the letter.
  Make sure your final transcription in the tool call MATCHES what you
  determined during thinking. Do not let the draft anchors override your
  own reading of the image.

imageKey: {image_key}
pageKey:  {page_key}

You MUST call submit_transcription with your complete transcription.\
"""


def build_transcription_request(image_key, page_key, image_b64, media_type,
                                 layout_scaffold, word_draft_ctx, bleedthrough_warning,
                                 column_order_instructions=""):
    prompt = TRANSCRIPTION_PROMPT_TEMPLATE.format(
        layout_scaffold=layout_scaffold,
        word_draft_context=word_draft_ctx,
        bleedthrough_warning=bleedthrough_warning,
        column_order_instructions=column_order_instructions,
        image_key=image_key,
        page_key=page_key
    )
    return {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": OPUS_MAX_TOKENS,
        "thinking": {
            "type":          "enabled",
            "budget_tokens": OPUS_THINKING_BUDGET
        },
        "tools": [TRANSCRIPTION_TOOL],
        "tool_choice": {"type": "auto"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": media_type,
                            "data":       image_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ]
    }


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    image_key       = force_string(event.get('imageKey'))
    page_key        = force_string(event.get('pageKey'))
    letter_id       = force_string(event.get('letterId'))
    textract_blocks = event.get('textractBlocks', [])

    if isinstance(textract_blocks, list) and textract_blocks and isinstance(textract_blocks[0], list):
        textract_blocks = textract_blocks[0]

    if not image_key or not page_key or not letter_id:
        raise ValueError(
            f"Missing required input. "
            f"imageKey={image_key}, pageKey={page_key}, letterId={letter_id}"
        )

    # 1. Load page image from S3
    s3_key = image_key if image_key.startswith("input/") else f"input/{image_key}"
    obj    = s3.get_object(Bucket=PIPELINE_BUCKET, Key=s3_key)
    raw    = obj['Body'].read()

    page_img     = Image.open(io.BytesIO(raw)).convert('RGB')
    img_w, img_h = page_img.size
    ext          = s3_key.lower().rsplit('.', 1)[-1]
    media_type   = MEDIA_TYPE_MAP.get(ext, 'image/jpeg')
    image_b64    = base64.b64encode(raw).decode('utf-8')
    print(f"[INFO] {letter_id}/{page_key} — image {img_w}×{img_h}px ({media_type})")

    # 2. Get LINE blocks with their WORD children
    lines_with_words = get_lines_with_words(textract_blocks)
    total_words = sum(len(lw['words']) for lw in lines_with_words)
    print(f"[INFO] {len(lines_with_words)} lines, {total_words} words in reading order")

    # 3. Extract Textract layout zones and tag each line with its zone.
    #    We emit word_positions for BODY lines only — header/ending lines
    #    still get pageLines anchors so the parser can slice the Opus output
    #    down to the body span before alignment.
    layout_zones = extract_layout_zones(textract_blocks)
    for lw in lines_with_words:
        lw['zone'] = classify_line_zone(lw['bbox'], layout_zones)

    zone_counts = {'body': 0, 'header': 0, 'ending': 0, 'other': 0}
    for lw in lines_with_words:
        zone_counts[lw['zone']] += 1
    print(f"[INFO] Line zones — body={zone_counts['body']}, "
          f"header={zone_counts['header']}, ending={zone_counts['ending']}, "
          f"other={zone_counts['other']}")

    # 4. Detect bleed-through across ALL words (zone-independent — noise is noise).
    all_word_blocks = []
    for lw in lines_with_words:
        all_word_blocks.extend(lw['words'])
    bleed_drop_ids, bleed_regions = detect_bleedthrough(all_word_blocks)

    # 5. Per line: Sonnet-transcribe BODY only; build pageLines anchors for all.
    word_positions  = []
    page_lines      = []
    global_word_idx = 0

    for lw in lines_with_words:
        line_bbox = lw['bbox']
        line_idx  = lw['line_index']
        words     = lw['words']
        zone      = lw['zone']

        line_geom = {
            "lineIndex": line_idx,
            "zone":      zone,
            "top":       round(line_bbox['Top'],    4),
            "left":      round(line_bbox['Left'],   4),
            "width":     round(line_bbox['Width'],  4),
            "height":    round(line_bbox['Height'], 4),
        }

        if zone != 'body':
            # Non-body line: Textract text is good enough for fuzzy anchoring.
            page_lines.append({
                **line_geom,
                "text": " ".join(w.get('Text', '') for w in words).strip(),
            })
            global_word_idx += len(words)
            continue

        # Body line — full Sonnet pipeline
        line_crop = crop_region(page_img, line_bbox, img_w, img_h, LINE_PADDING)

        active_words  = []
        bleed_indices = set()
        for wi, wb in enumerate(words):
            if wb['Id'] in bleed_drop_ids:
                bleed_indices.add(wi)
            else:
                active_words.append(wb)

        sonnet_texts = []
        if active_words and line_crop:
            sonnet_texts = transcribe_line(line_crop, active_words)
            print(f"[OK] Line {line_idx}: Sonnet transcribed {len(sonnet_texts)} words")
        elif active_words:
            sonnet_texts = [wb.get('Text', '[illegible]') for wb in active_words]
            print(f"[WARN] Line {line_idx}: line crop failed — Textract fallback")

        active_i = 0
        line_word_texts = []
        for wi, wb in enumerate(words):
            global_word_idx += 1
            bbox   = wb['Geometry']['BoundingBox']
            conf   = round(wb.get('Confidence', 0.0), 1)
            t_text = wb.get('Text', '')

            if wi in bleed_indices:
                draft  = t_text or '[bleedthrough]'
                source = "dropped_bleedthrough"
            elif active_i < len(sonnet_texts):
                draft  = sonnet_texts[active_i] or t_text or '[illegible]'
                source = "sonnet"
                active_i += 1
            else:
                draft  = t_text or '[illegible]'
                source = "textract_fallback"

            line_word_texts.append(draft)
            word_positions.append({
                "wordIndex":          global_word_idx,
                "textract_text":      t_text,
                "sonnet_text":        draft,
                "opus_text":          None,
                "final_text":         None,
                "textractConfidence": conf,
                "alignment_score":    0,
                "source":             source,
                "zone":               "body",
                "left":               round(bbox['Left'],   4),
                "top":                round(bbox['Top'],    4),
                "width":              round(bbox['Width'],  4),
                "height":             round(bbox['Height'], 4),
                "lineIndex":          line_idx,
                "lineTop":            round(line_bbox['Top'],    4),
                "lineLeft":           round(line_bbox['Left'],   4),
                "lineWidth":          round(line_bbox['Width'],  4),
                "lineHeight":         round(line_bbox['Height'], 4),
            })

        page_lines.append({
            **line_geom,
            "text": " ".join(line_word_texts).strip(),
        })

        if active_words and line_crop:
            time.sleep(0.2)  # rate limit between Sonnet calls

    bleed_count  = sum(1 for wp in word_positions if wp['source'] == 'dropped_bleedthrough')
    sonnet_count = sum(1 for wp in word_positions if wp['source'] == 'sonnet')
    active_count = len(word_positions) - bleed_count
    print(f"[INFO] {len(word_positions)} body word positions assembled — "
          f"{sonnet_count} sonnet, {active_count - sonnet_count} fallback, "
          f"{bleed_count} bleed-through dropped")

    # 6. Header-column detection still runs off the FULL Textract word set so
    #    the Opus prompt gets accurate column ordering for multi-column
    #    letterheads, even though header words are no longer word_positions.
    all_textract_wps = []
    for lw in lines_with_words:
        for wb in lw['words']:
            bb = wb['Geometry']['BoundingBox']
            all_textract_wps.append({
                'left':          round(bb['Left'],   4),
                'top':           round(bb['Top'],    4),
                'width':         round(bb['Width'],  4),
                'height':        round(bb['Height'], 4),
                'textract_text': wb.get('Text', ''),
                'sonnet_text':   wb.get('Text', ''),
            })
    header_columns   = detect_header_columns(all_textract_wps, layout_zones)
    col_instructions = build_column_order_instructions(header_columns)

    # 6. Build Opus transcription request
    bleedthrough_warning = build_bleedthrough_warning(bleed_regions)
    layout_scaffold      = build_layout_scaffold(textract_blocks)
    word_draft_ctx       = build_word_draft_context(word_positions)
    opus_request         = build_transcription_request(
        image_key, page_key, image_b64, media_type,
        layout_scaffold, word_draft_ctx, bleedthrough_warning,
        column_order_instructions=col_instructions
    )

    # 7. S3 key paths
    lid = safe_id(letter_id)
    pk  = safe_id(page_key)

    transcribe_payload_key = f"intermediate/payloads/{lid}/{pk}_transcribe_request.json"
    transcribe_output_key  = f"intermediate/transcribe-output/{lid}/{pk}_transcribe_result.json"
    word_positions_key     = f"intermediate/word-positions/{lid}/{pk}_word_positions.json"
    metadata_payload_key   = f"intermediate/payloads/{lid}_metadata_request.json"
    metadata_output_key    = f"intermediate/metadata-output/{lid}_metadata_result.json"
    reconcile_payload_key  = f"intermediate/reconcile-input/{lid}_reconcile_request.json"
    reconcile_output_key   = f"intermediate/reconcile-output/{lid}_reconcile_result.json"
    judge_payload_key      = f"intermediate/judge-input/{lid}_judge_request.json"
    judge_output_key       = f"intermediate/judge-output/{lid}_judge_result.json"

    # 8. Write word positions with layout zones + pageLines anchors embedded.
    #    pageLines covers ALL Textract LINE blocks (with zone tag) so the parser
    #    can locate the body span inside the full-page Opus transcription.
    word_positions_payload = {
        "words":         word_positions,
        "layoutZones":   layout_zones,
        "headerColumns": header_columns,
        "pageLines":     page_lines,
    }
    s3.put_object(
        Bucket=PIPELINE_BUCKET, Key=word_positions_key,
        Body=json.dumps(word_positions_payload, separators=(',', ':')),
        ContentType="application/json"
    )
    print(f"[OK] Word positions → {word_positions_key} ({len(word_positions)} body words, "
          f"{bleed_count} bleed-through tagged, {len(layout_zones)} layout zones, "
          f"{len(header_columns)} header columns, {len(page_lines)} page lines)")

    s3.put_object(
        Bucket=PIPELINE_BUCKET, Key=transcribe_payload_key,
        Body=json.dumps(opus_request),
        ContentType="application/json"
    )
    print(f"[OK] Opus payload → {transcribe_payload_key}")

    return {
        "imageKey":             image_key,
        "pageKey":              page_key,
        "letterId":             letter_id,
        "inputS3Uri":           f"s3://{PIPELINE_BUCKET}/{transcribe_payload_key}",
        "outputS3Uri":          f"s3://{PIPELINE_BUCKET}/{transcribe_output_key}",
        "wordPositionsS3Uri":   f"s3://{PIPELINE_BUCKET}/{word_positions_key}",
        "metadataInputS3Uri":   f"s3://{PIPELINE_BUCKET}/{metadata_payload_key}",
        "metadataOutputS3Uri":  f"s3://{PIPELINE_BUCKET}/{metadata_output_key}",
        "reconcileInputS3Uri":  f"s3://{PIPELINE_BUCKET}/{reconcile_payload_key}",
        "reconcileOutputS3Uri": f"s3://{PIPELINE_BUCKET}/{reconcile_output_key}",
        "judgeInputS3Uri":      f"s3://{PIPELINE_BUCKET}/{judge_payload_key}",
        "judgeOutputS3Uri":     f"s3://{PIPELINE_BUCKET}/{judge_output_key}",
        "intermediateBucket":   PIPELINE_BUCKET
    }