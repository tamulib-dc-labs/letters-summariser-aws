import boto3
import json
import re


s3 = boto3.client('s3', region_name='us-east-2')


PIPELINE_BUCKET               = "cursive-letters-pipeline"
TEXTRACT_CONFIDENCE_THRESHOLD = 80.0
TEXTRACT_OPUS_GUARD           = 85.0   # when Textract+Sonnet agree at this confidence,
                                       # don't let Opus override (protects printed text)
HEADER_PROTECT_THRESHOLD      = 92.0   # in header zones, protect high-confidence printed text
                                       # more aggressively from Opus overrides


# ─── Needleman-Wunsch scoring ────────────────────────────────────────────────
NW_MATCH_SCORE    =  2.0
NW_MISMATCH_COST  = -1.0
NW_GAP_OPEN       = -2.0
NW_GAP_EXTEND     = -0.5
NW_FUZZY_THRESH   = 55.0


# ─── Spatial proximity thresholds ────────────────────────────────────────────
ENDING_SPATIAL_Y_THRESHOLD = 0.02   # 2% page height — for ending zone proximity match
ENDING_SPATIAL_X_THRESHOLD = 0.05   # 5% page width


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


def extract_json_from_text(text):
    text = re.sub(r'```(?:json)?\s*', '', text).strip()
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise ValueError("No valid JSON object found in Claude response")
    return json.loads(match.group())


def page_number_from_key(page_key):
    m = re.search(r'(\d+)', page_key)
    return int(m.group(1)) if m else 1


# ─── Bedrock response parser ──────────────────────────────────────────────────

def extract_thinking_and_tool_result(raw, expected_tool_name=None):
    if 'Body' in raw and isinstance(raw['Body'], dict) and 'content' in raw['Body']:
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
        has_sig = bool(thinking_block and thinking_block.get('signature'))
        print(
            f"[OK] tool={tool_use_block.get('name')}, "
            f"thinking={'yes, signature=' + str(has_sig) if thinking_block else 'no'}"
        )
        return thinking_block, tool_input

    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text', '').strip()
            if text:
                print(f"[WARN] No tool_use (stop_reason={stop_reason}) — falling back to text parse")
                return thinking_block, extract_json_from_text(text)

    raise ValueError(
        f"No tool_use or text block found. "
        f"stop_reason={stop_reason}, block_types={block_types}"
    )


# ─── Fuzzy ratio (rapidfuzz with pure-Python fallback) ────────────────────────

def _fuzzy_ratio(a: str, b: str) -> float:
    try:
        from rapidfuzz.fuzz import ratio
        return ratio(a, b)
    except ImportError:
        if not a and not b:
            return 100.0
        if not a or not b:
            return 0.0
        longer  = max(len(a), len(b))
        matches = sum(c1 == c2 for c1, c2 in zip(a, b))
        return (matches / longer) * 100.0


def _normalise(text: str) -> str:
    return (text or '').lower().strip('.,;:!?-—\'"()[]{}')


# ─── Tokenisation ─────────────────────────────────────────────────────────────

def tokenise_transcription(text: str) -> list:
    tokens      = []
    token_idx   = 0
    lines       = text.split('\n')
    char_offset = 0

    for line_idx, line in enumerate(lines):
        for m in re.finditer(r'\S+', line):
            raw   = m.group()
            start = char_offset + m.start()
            end   = char_offset + m.end()
            norm  = _normalise(re.sub(r'\[\?\]$', '', raw))

            tokens.append({
                "token_index": token_idx,
                "raw":         raw,
                "norm":        norm,
                "char_start":  start,
                "char_end":    end,
                "line_index":  line_idx,
            })
            token_idx += 1

        char_offset += len(line) + 1

    return tokens


def tokenise_by_line(text: str) -> list:
    """
    Split transcription into lines, each line into word tokens.
    Returns list of lists: [[{token}, ...], ...]
    """
    all_tokens = tokenise_transcription(text)
    if not all_tokens:
        return []

    lines = []
    current_line_idx = all_tokens[0]['line_index']
    current_line = []

    for t in all_tokens:
        if t['line_index'] != current_line_idx:
            lines.append(current_line)
            current_line = []
            current_line_idx = t['line_index']
        current_line.append(t)

    if current_line:
        lines.append(current_line)

    return lines


# ─── Zone detection ──────────────────────────────────────────────────────────

def classify_word_zone(wp: dict, layout_zones: list) -> str:
    """
    Classify a single word position into 'header', 'body', or 'ending' zone
    using Textract LAYOUT block boundaries.
    """
    wp_cy = wp.get('top', 0) + wp.get('height', 0) / 2
    wp_cx = wp.get('left', 0) + wp.get('width', 0) / 2

    for zone in layout_zones:
        zt = zone.get('type', '')
        if (zone['top'] <= wp_cy <= zone['bottom'] and
            zone['left'] <= wp_cx <= zone['right']):
            if zt == 'LAYOUT_HEADER':
                return 'header'
            elif zt == 'LAYOUT_FOOTER':
                return 'ending'
    return 'body'


def tag_word_zones(word_positions: list, layout_zones: list) -> None:
    """
    Tag each word position with its zone: 'header', 'body', or 'ending'.
    Modifies word_positions in place.
    """
    if not layout_zones:
        # No layout info — all words are body zone
        for wp in word_positions:
            wp['zone'] = 'body'
        return

    for wp in word_positions:
        wp['zone'] = classify_word_zone(wp, layout_zones)

    header_count = sum(1 for wp in word_positions if wp['zone'] == 'header')
    ending_count = sum(1 for wp in word_positions if wp['zone'] == 'ending')
    body_count   = sum(1 for wp in word_positions if wp['zone'] == 'body')
    print(f"[INFO] Zone classification: {header_count} header, {body_count} body, {ending_count} ending")


# ─── Header column reordering ────────────────────────────────────────────────

def reorder_header_words_by_column(header_wps: list, header_columns: list) -> list:
    """
    Reorder header word positions column-by-column (left→right) to match
    Opus's reading order.  Within each column, sort by vertical position.

    header_wps:      list of word_position dicts tagged with zone='header'
    header_columns:  list of {left, right, word_count, approx_content} from payload generator

    Returns reordered list of word positions.
    """
    if not header_columns or len(header_columns) < 2:
        # No multi-column layout — sort by line then word index
        return sorted(header_wps, key=lambda w: (w.get('lineIndex', 0), w.get('wordIndex', 0)))

    # Assign each word to a column based on its center X
    col_buckets = [[] for _ in header_columns]
    unassigned  = []

    for wp in header_wps:
        wp_cx = wp['left'] + wp['width'] / 2
        assigned = False
        for ci, col in enumerate(header_columns):
            if col['left'] - 0.02 <= wp_cx <= col['right'] + 0.02:
                col_buckets[ci].append(wp)
                assigned = True
                break
        if not assigned:
            # Word doesn't fall in any column — assign to nearest
            min_dist = float('inf')
            best_ci  = 0
            for ci, col in enumerate(header_columns):
                col_center = (col['left'] + col['right']) / 2
                dist = abs(wp_cx - col_center)
                if dist < min_dist:
                    min_dist = dist
                    best_ci = ci
            col_buckets[best_ci].append(wp)

    # Sort within each column by vertical position (top → bottom)
    for bucket in col_buckets:
        bucket.sort(key=lambda w: (w.get('top', 0), w.get('left', 0)))

    # Concatenate columns left→right
    reordered = []
    for ci, bucket in enumerate(col_buckets):
        reordered.extend(bucket)

    return reordered


# ─── Spatial proximity matching for ending zone ──────────────────────────────

def find_nearest_wp_by_position(opus_word_top: float, opus_word_left: float,
                                 candidate_wps: list) -> tuple:
    """
    Find the nearest unmatched word position by spatial proximity.
    Returns (best_wp_index, distance) or (None, inf).
    """
    best_idx  = None
    best_dist = float('inf')

    for i, wp in enumerate(candidate_wps):
        if wp.get('final_text') is not None:
            continue  # already matched
        if wp.get('source') == 'dropped_bleedthrough':
            continue

        dy = abs(opus_word_top - wp.get('top', 0))
        dx = abs(opus_word_left - wp.get('left', 0))

        if dy <= ENDING_SPATIAL_Y_THRESHOLD and dx <= ENDING_SPATIAL_X_THRESHOLD:
            dist = (dy ** 2 + dx ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_idx  = i

    return best_idx, best_dist


# ─── Needleman-Wunsch word alignment within a line ──────────────────────────

def _nw_score(token_norm: str, wp_norm: str) -> float:
    ratio = _fuzzy_ratio(token_norm, wp_norm)
    if ratio >= NW_FUZZY_THRESH:
        return NW_MATCH_SCORE * (ratio - NW_FUZZY_THRESH) / (100.0 - NW_FUZZY_THRESH)
    else:
        return NW_MISMATCH_COST


def _nw_align_words(opus_words: list, sonnet_words: list) -> list:
    """
    Needleman-Wunsch alignment with affine gap penalties.
    opus_words:   list of token dicts (from Opus transcription)
    sonnet_words: list of word_position dicts (Sonnet-corrected, with positions)

    Returns list of (opus_local_idx, sonnet_local_idx_or_None, score).
    """
    if not opus_words:
        return []
    if not sonnet_words:
        return [(i, None, 0.0) for i in range(len(opus_words))]

    n = len(opus_words)
    m = len(sonnet_words)

    # Precompute substitution scores
    sub_score = [[0.0] * m for _ in range(n)]
    for i in range(n):
        t_norm = opus_words[i]['norm']
        for j in range(m):
            # Compare against Sonnet-corrected text
            w_norm = _normalise(sonnet_words[j].get('sonnet_text', ''))
            sub_score[i][j] = _nw_score(t_norm, w_norm)

    INF = float('inf')

    M = [[-INF] * (m + 1) for _ in range(n + 1)]
    X = [[-INF] * (m + 1) for _ in range(n + 1)]
    Y = [[-INF] * (m + 1) for _ in range(n + 1)]

    M[0][0] = 0.0
    for j in range(1, m + 1):
        Y[0][j] = NW_GAP_OPEN + NW_GAP_EXTEND * (j - 1)
    for i in range(1, n + 1):
        X[i][0] = NW_GAP_OPEN + NW_GAP_EXTEND * (i - 1)

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best_prev = max(M[i-1][j-1], X[i-1][j-1], Y[i-1][j-1])
            M[i][j] = best_prev + sub_score[i-1][j-1]

            X[i][j] = max(
                M[i-1][j] + NW_GAP_OPEN,
                X[i-1][j] + NW_GAP_EXTEND,
                Y[i-1][j] + NW_GAP_OPEN,
            )

            Y[i][j] = max(
                M[i][j-1] + NW_GAP_OPEN,
                X[i][j-1] + NW_GAP_OPEN,
                Y[i][j-1] + NW_GAP_EXTEND,
            )

    end_scores = {'M': M[n][m], 'X': X[n][m], 'Y': Y[n][m]}
    best_end = max(end_scores.values())
    if best_end == -INF:
        return [(i, None, 0.0) for i in range(n)]
    state = max(end_scores, key=end_scores.get)

    alignment = []
    i, j = n, m

    while i > 0 or j > 0:
        if state == 'M' and i > 0 and j > 0:
            alignment.append(('match', i - 1, j - 1))
            prev = {'M': M[i-1][j-1], 'X': X[i-1][j-1], 'Y': Y[i-1][j-1]}
            state = max(prev, key=prev.get)
            i -= 1
            j -= 1
        elif state == 'X' and i > 0:
            alignment.append(('ins_token', i - 1, None))
            prev = {
                'M': M[i-1][j] + NW_GAP_OPEN,
                'X': X[i-1][j] + NW_GAP_EXTEND,
                'Y': Y[i-1][j] + NW_GAP_OPEN,
            }
            state = max(prev, key=prev.get)
            i -= 1
        elif j > 0:
            alignment.append(('skip_wp', None, j - 1))
            prev = {
                'M': M[i][j-1] + NW_GAP_OPEN,
                'X': X[i][j-1] + NW_GAP_OPEN,
                'Y': Y[i][j-1] + NW_GAP_EXTEND,
            }
            state = max(prev, key=prev.get)
            j -= 1
        else:
            break

    alignment.reverse()

    result = []
    for op, ti, wj in alignment:
        if op == 'match':
            score = (sub_score[ti][wj] / NW_MATCH_SCORE * 100.0) if NW_MATCH_SCORE > 0 else 0.0
            score = max(0.0, score)
            result.append((ti, wj, score))
        elif op == 'ins_token':
            result.append((ti, None, 0.0))

    return result


# ─── Line-level NW alignment (Opus lines ↔ Sonnet/Textract lines) ───────────

def _line_text(words: list, key: str) -> str:
    """Join word texts from a list of dicts using the given key."""
    return ' '.join(_normalise(w.get(key, '')) for w in words)


def _nw_align_lines(opus_lines: list, textract_lines: list) -> list:
    """
    Align Opus transcription lines to Textract/Sonnet lines.
    opus_lines:     list of lists of token dicts
    textract_lines: list of lists of word_position dicts

    Returns list of (opus_line_idx, textract_line_idx_or_None, score).
    Uses the same NW algorithm but at line granularity, comparing
    full line text via fuzzy ratio.
    """
    if not opus_lines:
        return []
    if not textract_lines:
        return [(i, None, 0.0) for i in range(len(opus_lines))]

    n = len(opus_lines)
    m = len(textract_lines)

    # Precompute line-level fuzzy scores
    sub_score = [[0.0] * m for _ in range(n)]
    for i in range(n):
        opus_text = ' '.join(t['norm'] for t in opus_lines[i])
        for j in range(m):
            tx_text = _line_text(textract_lines[j], 'sonnet_text')
            ratio = _fuzzy_ratio(opus_text, tx_text)
            if ratio >= NW_FUZZY_THRESH:
                sub_score[i][j] = NW_MATCH_SCORE * (ratio - NW_FUZZY_THRESH) / (100.0 - NW_FUZZY_THRESH)
            else:
                sub_score[i][j] = NW_MISMATCH_COST

    INF = float('inf')

    M_mat = [[-INF] * (m + 1) for _ in range(n + 1)]
    X_mat = [[-INF] * (m + 1) for _ in range(n + 1)]
    Y_mat = [[-INF] * (m + 1) for _ in range(n + 1)]

    M_mat[0][0] = 0.0
    for j in range(1, m + 1):
        Y_mat[0][j] = NW_GAP_OPEN + NW_GAP_EXTEND * (j - 1)
    for i in range(1, n + 1):
        X_mat[i][0] = NW_GAP_OPEN + NW_GAP_EXTEND * (i - 1)

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best_prev = max(M_mat[i-1][j-1], X_mat[i-1][j-1], Y_mat[i-1][j-1])
            M_mat[i][j] = best_prev + sub_score[i-1][j-1]
            X_mat[i][j] = max(
                M_mat[i-1][j] + NW_GAP_OPEN,
                X_mat[i-1][j] + NW_GAP_EXTEND,
                Y_mat[i-1][j] + NW_GAP_OPEN,
            )
            Y_mat[i][j] = max(
                M_mat[i][j-1] + NW_GAP_OPEN,
                X_mat[i][j-1] + NW_GAP_OPEN,
                Y_mat[i][j-1] + NW_GAP_EXTEND,
            )

    end_scores = {'M': M_mat[n][m], 'X': X_mat[n][m], 'Y': Y_mat[n][m]}
    best_end = max(end_scores.values())
    if best_end == -INF:
        return [(i, None, 0.0) for i in range(n)]
    state = max(end_scores, key=end_scores.get)

    alignment = []
    i, j = n, m
    while i > 0 or j > 0:
        if state == 'M' and i > 0 and j > 0:
            alignment.append(('match', i - 1, j - 1))
            prev = {'M': M_mat[i-1][j-1], 'X': X_mat[i-1][j-1], 'Y': Y_mat[i-1][j-1]}
            state = max(prev, key=prev.get)
            i -= 1
            j -= 1
        elif state == 'X' and i > 0:
            alignment.append(('ins', i - 1, None))
            prev = {
                'M': M_mat[i-1][j] + NW_GAP_OPEN,
                'X': X_mat[i-1][j] + NW_GAP_EXTEND,
                'Y': Y_mat[i-1][j] + NW_GAP_OPEN,
            }
            state = max(prev, key=prev.get)
            i -= 1
        elif j > 0:
            alignment.append(('skip', None, j - 1))
            prev = {
                'M': M_mat[i][j-1] + NW_GAP_OPEN,
                'X': X_mat[i][j-1] + NW_GAP_OPEN,
                'Y': Y_mat[i][j-1] + NW_GAP_EXTEND,
            }
            state = max(prev, key=prev.get)
            j -= 1
        else:
            break

    alignment.reverse()

    result = []
    for op, oi, tj in alignment:
        if op == 'match':
            score = (sub_score[oi][tj] / NW_MATCH_SCORE * 100.0) if NW_MATCH_SCORE > 0 else 0.0
            result.append((oi, tj, max(0.0, score)))
        elif op == 'ins':
            result.append((oi, None, 0.0))

    return result


# ─── Two-level alignment: line match → word match ───────────────────────────

def find_body_opus_span(opus_lines: list, page_lines: list) -> tuple:
    """
    Use pageLines anchors to locate the [start, end) Opus line range that
    corresponds to the body of the letter.

    Opus transcribes the whole page (header + body + footer); word_positions
    now contain BODY only. We need to slice Opus text down to its body span
    before alignment, otherwise header/footer Opus lines would be aligned to
    body word_positions and corrupt the result.

    Returns (start_idx, end_idx). Defaults to the full Opus range when there
    are no pre/post-body anchors to match against.
    """
    if not page_lines or not opus_lines:
        return 0, len(opus_lines)

    body_lines = [
        pl for pl in page_lines
        if pl.get('zone') == 'body' and pl.get('text', '').strip()
    ]
    if not body_lines:
        return 0, len(opus_lines)

    # Locate first/last body anchors within the pageLines stream
    body_first = body_lines[0]
    body_last  = body_lines[-1]
    pre_body  = [pl for pl in page_lines
                 if pl.get('top', 1.0) < body_first.get('top', 0.0)
                 and pl.get('zone') in ('header', 'other')
                 and pl.get('text', '').strip()]
    post_body = [pl for pl in page_lines
                 if pl.get('top', 0.0) > body_last.get('top', 1.0)
                 and pl.get('zone') in ('ending', 'other')
                 and pl.get('text', '').strip()]

    body_start = 0
    body_end   = len(opus_lines)

    # Find body start in Opus by fuzzy-matching the first body line text
    if pre_body:
        first_body_text = body_first['text']
        for oi, line_tokens in enumerate(opus_lines):
            line_text = ' '.join(t['norm'] for t in line_tokens)
            if _fuzzy_ratio(line_text, first_body_text) >= 50.0:
                body_start = oi
                break

    # Find body end in Opus by fuzzy-matching the first ending line text
    if post_body:
        first_ending_text = post_body[0]['text']
        for oi in range(len(opus_lines) - 1, body_start, -1):
            line_text = ' '.join(t['norm'] for t in opus_lines[oi])
            if _fuzzy_ratio(line_text, first_ending_text) >= 50.0:
                body_end = oi
                break

    return body_start, body_end


def align_opus_to_positions(opus_text: str, word_positions: list, page_number: int,
                            layout_zones: list = None, header_columns: list = None,
                            page_lines: list = None) -> tuple:
    """
    Zone-aware two-level alignment:
      1. Tag each word position with its zone (header/body/ending)
      2. For HEADER zone: reorder words column-by-column, run NW on reordered
         sequence, and protect high-confidence printed text
      3. For BODY zone: standard two-level NW (line match → word match)
      4. For ENDING zone: standard NW with spatial proximity tiebreaker

    Opus text wins for final transcription. Textract bounding boxes provide
    word positions. Sonnet corrections bridge the two.
    """
    for wp in word_positions:
        wp['page'] = page_number

    # Tag zones
    tag_word_zones(word_positions, layout_zones or [])

    # Partition word positions by zone
    header_wps = [wp for wp in word_positions if wp.get('zone') == 'header'
                  and wp.get('source') != 'dropped_bleedthrough']
    body_wps   = [wp for wp in word_positions if wp.get('zone') == 'body']
    ending_wps = [wp for wp in word_positions if wp.get('zone') == 'ending'
                  and wp.get('source') != 'dropped_bleedthrough']

    # We'll determine the approximate vertical boundary between header and body
    # to split the Opus transcription text into zone segments
    header_bottom = 0.0
    ending_top    = 1.0

    if header_wps:
        header_bottom = max(wp.get('top', 0) + wp.get('height', 0) for wp in header_wps)
    if ending_wps:
        ending_top = min(wp.get('top', 1.0) for wp in ending_wps)

    has_header_zone = len(header_wps) > 0 and header_columns and len(header_columns) >= 2
    has_ending_zone = len(ending_wps) > 0

    print(f"[INFO] Zone-aware alignment: header={len(header_wps)} words "
          f"({len(header_columns or [])} columns), body={len(body_wps)} words, "
          f"ending={len(ending_wps)} words")

    # ═══════════════════════════════════════════════════════════════════════
    #  HEADER ZONE — column-reordered alignment with printed text protection
    # ═══════════════════════════════════════════════════════════════════════
    if has_header_zone:
        # Reorder header words column-by-column to match Opus reading order
        reordered_header = reorder_header_words_by_column(header_wps, header_columns)

        # Tokenise Opus text and estimate which tokens belong to header
        # We use line-level heuristic: header Opus lines are at the start,
        # before the first body line that matches body content
        opus_lines = tokenise_by_line(opus_text)

        # Try to find the split point: the line where body content starts
        # by checking which Opus lines match body vs header content
        body_wps_active = [wp for wp in body_wps if wp.get('source') != 'dropped_bleedthrough']
        body_lines_by_idx = {}
        for wp in body_wps_active:
            li = wp.get('lineIndex', -1)
            if li not in body_lines_by_idx:
                body_lines_by_idx[li] = []
            body_lines_by_idx[li].append(wp)
        body_textract_lines = [body_lines_by_idx[k] for k in sorted(body_lines_by_idx.keys())]

        # Find Opus header/body split
        # Score each Opus line against first few body Textract lines
        header_opus_end = 0
        if body_textract_lines and opus_lines:
            first_body_text = _line_text(body_textract_lines[0], 'sonnet_text')
            for oi, opus_line_tokens in enumerate(opus_lines):
                opus_line_text = ' '.join(t['norm'] for t in opus_line_tokens)
                ratio = _fuzzy_ratio(opus_line_text, first_body_text)
                if ratio >= 50.0:
                    header_opus_end = oi
                    break
            else:
                # If no match found, estimate based on proportion
                if len(word_positions) > 0:
                    header_frac = len(header_wps) / len(word_positions)
                    header_opus_end = max(1, int(len(opus_lines) * header_frac))
                else:
                    header_opus_end = 0

        # Flatten header opus tokens
        header_opus_tokens = []
        for line_tokens in opus_lines[:header_opus_end]:
            header_opus_tokens.extend(line_tokens)

        if header_opus_tokens and reordered_header:
            # Run word-level NW on the column-reordered header words
            word_pairs = _nw_align_words(header_opus_tokens, reordered_header)

            for local_opus_i, local_wp_i, word_score in word_pairs:
                if local_wp_i is None:
                    continue

                token = header_opus_tokens[local_opus_i]
                wp    = reordered_header[local_wp_i]

                opus_word    = token['raw']
                sonnet_text  = wp.get('sonnet_text', '')
                sonnet_norm  = _normalise(sonnet_text)
                opus_norm    = _normalise(re.sub(r'\[\?\]$', '', opus_word))
                textract_norm = _normalise(wp.get('textract_text', ''))
                t_conf        = wp.get('textractConfidence', 0.0)

                wp['opus_text']       = opus_word
                wp['alignment_score'] = round(word_score, 1)

                # Header protection: printed text is reliably read by Textract
                # So when Textract+Sonnet agree at high confidence, ALWAYS trust them
                sonnet_textract_agree = _fuzzy_ratio(sonnet_norm, textract_norm) >= 80
                if sonnet_textract_agree and t_conf >= HEADER_PROTECT_THRESHOLD:
                    wp['source']     = 'sonnet_confirmed'
                    wp['final_text'] = sonnet_text
                elif word_score >= 82:
                    wp['source']     = 'sonnet_confirmed'
                    wp['final_text'] = opus_word
                elif _fuzzy_ratio(opus_norm, sonnet_norm) < 60:
                    # In header zone, be more conservative — don't let Opus override
                    if sonnet_textract_agree and t_conf >= TEXTRACT_OPUS_GUARD:
                        wp['source']     = 'sonnet_confirmed'
                        wp['final_text'] = sonnet_text
                    else:
                        wp['source']     = 'opus_corrected'
                        wp['final_text'] = opus_word
                else:
                    wp['source']     = 'sonnet_aligned'
                    wp['final_text'] = opus_word

            aligned_header = sum(1 for wp in reordered_header if wp.get('final_text') is not None)
            print(f"[INFO] Header alignment: {aligned_header}/{len(reordered_header)} words matched")

    # ═══════════════════════════════════════════════════════════════════════
    #  BODY ZONE — standard two-level NW (unchanged logic)
    # ═══════════════════════════════════════════════════════════════════════

    # Group body word_positions by lineIndex
    lines_by_idx = {}
    for wp in body_wps:
        li = wp.get('lineIndex', -1)
        if li not in lines_by_idx:
            lines_by_idx[li] = []
        lines_by_idx[li].append(wp)

    # Build textract_lines in order, then merge lines at similar vertical positions.
    LINE_MERGE_THRESHOLD = 0.015  # lines within 1.5% page height are same row
    raw_lines = [lines_by_idx[k] for k in sorted(lines_by_idx.keys())]

    merged_lines = []
    for line_wps in raw_lines:
        line_top = line_wps[0].get('lineTop', line_wps[0].get('top', 0))
        merged = False
        if merged_lines:
            prev_top = merged_lines[-1][0].get('lineTop', merged_lines[-1][0].get('top', 0))
            if abs(line_top - prev_top) < LINE_MERGE_THRESHOLD:
                merged_lines[-1].extend(line_wps)
                merged_lines[-1].sort(key=lambda wp: wp.get('left', 0))
                merged = True
        if not merged:
            merged_lines.append(list(line_wps))

    if len(merged_lines) < len(raw_lines):
        print(f"[INFO] Merged {len(raw_lines)} Textract lines → {len(merged_lines)} visual rows")

    # Filter out lines that are entirely bleed-through
    active_textract_lines = []
    for line_wps in merged_lines:
        active = [wp for wp in line_wps if wp.get('source') != 'dropped_bleedthrough']
        if active:
            active_textract_lines.append(line_wps)

    # Tokenise Opus output by line. Preferred path: use pageLines anchors
    # (covers header/body/ending in one pass) to slice the body span. Fall
    # back to legacy header_opus_end split when no pageLines were emitted.
    opus_lines_all = tokenise_by_line(opus_text)
    pagelines_used = False
    ending_opus_lines = []

    if page_lines:
        body_start_oi, body_end_oi = find_body_opus_span(opus_lines_all, page_lines)
        body_opus_lines   = opus_lines_all[body_start_oi:body_end_oi]
        ending_opus_lines = opus_lines_all[body_end_oi:]
        pagelines_used    = True
        print(f"[INFO] Opus body span via pageLines: lines [{body_start_oi}:{body_end_oi}] "
              f"of {len(opus_lines_all)}")
    elif has_header_zone:
        body_opus_lines = opus_lines_all[header_opus_end:]
    else:
        body_opus_lines = opus_lines_all

    # Legacy fallback: re-split body vs ending using ending_wps when pageLines
    # didn't provide the anchors. Skipped when pageLines already partitioned.
    if not pagelines_used and has_ending_zone and body_opus_lines:
        # Score last few Opus lines against ending Textract words
        ending_lines_by_idx = {}
        for wp in ending_wps:
            li = wp.get('lineIndex', -1)
            if li not in ending_lines_by_idx:
                ending_lines_by_idx[li] = []
            ending_lines_by_idx[li].append(wp)
        ending_textract_lines = [ending_lines_by_idx[k] for k in sorted(ending_lines_by_idx.keys())]

        if ending_textract_lines:
            first_ending_text = _line_text(ending_textract_lines[0], 'sonnet_text')
            body_end_idx = len(body_opus_lines)
            for oi in range(len(body_opus_lines) - 1, max(0, len(body_opus_lines) - 10) - 1, -1):
                opus_line_text = ' '.join(t['norm'] for t in body_opus_lines[oi])
                ratio = _fuzzy_ratio(opus_line_text, first_ending_text)
                if ratio >= 50.0:
                    body_end_idx = oi
                    break
            ending_opus_lines = body_opus_lines[body_end_idx:]
            body_opus_lines   = body_opus_lines[:body_end_idx]

    print(f"[INFO] Body line alignment: {len(body_opus_lines)} Opus lines ↔ "
          f"{len(active_textract_lines)} Textract lines (page {page_number})")

    # Level 1: line-level NW alignment
    line_pairs = _nw_align_lines(body_opus_lines, active_textract_lines)

    matched_lines   = 0
    unmatched_opus  = 0

    for opus_li, tx_li, line_score in line_pairs:
        opus_tokens = body_opus_lines[opus_li]

        if tx_li is None:
            unmatched_opus += 1
            continue

        tx_wps = active_textract_lines[tx_li]
        matched_lines += 1

        # Level 2: word-level NW alignment within this line pair
        active_wps = [wp for wp in tx_wps if wp.get('source') != 'dropped_bleedthrough']
        word_pairs = _nw_align_words(opus_tokens, active_wps)

        for local_opus_i, local_wp_i, word_score in word_pairs:
            if local_wp_i is None:
                continue

            token = opus_tokens[local_opus_i]
            wp    = active_wps[local_wp_i]

            opus_word    = token['raw']
            sonnet_text  = wp.get('sonnet_text', '')
            sonnet_norm  = _normalise(sonnet_text)
            opus_norm    = _normalise(re.sub(r'\[\?\]$', '', opus_word))
            textract_norm = _normalise(wp.get('textract_text', ''))
            t_conf        = wp.get('textractConfidence', 0.0)

            wp['opus_text']       = opus_word
            wp['alignment_score'] = round(word_score, 1)

            if word_score >= 82:
                wp['source']     = 'sonnet_confirmed'
                wp['final_text'] = opus_word
            elif _fuzzy_ratio(opus_norm, sonnet_norm) < 60:
                sonnet_textract_agree = _fuzzy_ratio(sonnet_norm, textract_norm) >= 80
                if sonnet_textract_agree and t_conf >= TEXTRACT_OPUS_GUARD:
                    wp['source']     = 'sonnet_confirmed'
                    wp['final_text'] = sonnet_text
                else:
                    wp['source']     = 'opus_corrected'
                    wp['final_text'] = opus_word
            else:
                wp['source']     = 'sonnet_aligned'
                wp['final_text'] = opus_word

    # ═══════════════════════════════════════════════════════════════════════
    #  ENDING ZONE — NW with spatial proximity tiebreaker
    # ═══════════════════════════════════════════════════════════════════════
    if has_ending_zone and ending_opus_lines:
        # Build ending Textract lines
        ending_lines_by_idx = {}
        for wp in ending_wps:
            li = wp.get('lineIndex', -1)
            if li not in ending_lines_by_idx:
                ending_lines_by_idx[li] = []
            ending_lines_by_idx[li].append(wp)
        ending_textract_lines = [ending_lines_by_idx[k] for k in sorted(ending_lines_by_idx.keys())]

        # Filter bleed-through
        active_ending_lines = []
        for line_wps in ending_textract_lines:
            active = [wp for wp in line_wps if wp.get('source') != 'dropped_bleedthrough']
            if active:
                active_ending_lines.append(line_wps)

        # Line-level alignment
        ending_line_pairs = _nw_align_lines(ending_opus_lines, active_ending_lines)

        for opus_li, tx_li, line_score in ending_line_pairs:
            opus_tokens = ending_opus_lines[opus_li]

            if tx_li is None:
                # For single-word Opus lines in ending (like "H" signature),
                # try spatial proximity match
                if len(opus_tokens) == 1:
                    token = opus_tokens[0]
                    # We don't have exact spatial position for Opus tokens,
                    # but we can estimate: ending tokens are near the bottom
                    # Use the ending zone Textract words as candidates
                    for wp in ending_wps:
                        if wp.get('final_text') is not None:
                            continue
                        if wp.get('source') == 'dropped_bleedthrough':
                            continue
                        opus_norm = _normalise(re.sub(r'\[\?\]$', '', token['raw']))
                        sonnet_norm = _normalise(wp.get('sonnet_text', ''))
                        if _fuzzy_ratio(opus_norm, sonnet_norm) >= 60:
                            wp['opus_text']       = token['raw']
                            wp['alignment_score'] = round(_fuzzy_ratio(opus_norm, sonnet_norm), 1)
                            wp['source']          = 'sonnet_aligned'
                            wp['final_text']      = token['raw']
                            break
                continue

            tx_wps = active_ending_lines[tx_li]
            active_wps = [wp for wp in tx_wps if wp.get('source') != 'dropped_bleedthrough']
            word_pairs = _nw_align_words(opus_tokens, active_wps)

            for local_opus_i, local_wp_i, word_score in word_pairs:
                if local_wp_i is None:
                    continue

                token = opus_tokens[local_opus_i]
                wp    = active_wps[local_wp_i]

                opus_word    = token['raw']
                sonnet_text  = wp.get('sonnet_text', '')
                sonnet_norm  = _normalise(sonnet_text)
                opus_norm    = _normalise(re.sub(r'\[\?\]$', '', opus_word))
                textract_norm = _normalise(wp.get('textract_text', ''))
                t_conf        = wp.get('textractConfidence', 0.0)

                wp['opus_text']       = opus_word
                wp['alignment_score'] = round(word_score, 1)

                if word_score >= 82:
                    wp['source']     = 'sonnet_confirmed'
                    wp['final_text'] = opus_word
                elif _fuzzy_ratio(opus_norm, sonnet_norm) < 60:
                    sonnet_textract_agree = _fuzzy_ratio(sonnet_norm, textract_norm) >= 80
                    if sonnet_textract_agree and t_conf >= TEXTRACT_OPUS_GUARD:
                        wp['source']     = 'sonnet_confirmed'
                        wp['final_text'] = sonnet_text
                    else:
                        wp['source']     = 'opus_corrected'
                        wp['final_text'] = opus_word
                else:
                    wp['source']     = 'sonnet_aligned'
                    wp['final_text'] = opus_word

        ending_matched = sum(1 for wp in ending_wps if wp.get('final_text') is not None)
        print(f"[INFO] Ending alignment: {ending_matched}/{len(ending_wps)} words matched")

    # ═══════════════════════════════════════════════════════════════════════
    #  Tag remaining unmatched word positions
    # ═══════════════════════════════════════════════════════════════════════
    for wp in word_positions:
        if wp.get('source') in ('dropped_bleedthrough',):
            continue
        if wp.get('final_text') is not None:
            continue

        conf = wp.get('textractConfidence', 0.0)
        zone = wp.get('zone', 'body')

        if zone == 'header' and conf >= TEXTRACT_CONFIDENCE_THRESHOLD:
            # Header words with no Opus match — Textract/Sonnet is reliable
            wp['source']     = 'unmatched_textract'
            wp['final_text'] = wp.get('sonnet_text', '')
        elif conf >= TEXTRACT_CONFIDENCE_THRESHOLD:
            wp['source']     = 'unmatched_textract'
            wp['final_text'] = wp.get('sonnet_text', '')
        else:
            wp['source']     = 'dropped_noise'
            wp['final_text'] = None

    # Stats
    confirmed  = sum(1 for wp in word_positions if wp.get('source') == 'sonnet_confirmed')
    aligned    = sum(1 for wp in word_positions if wp.get('source') == 'sonnet_aligned')
    corrected  = sum(1 for wp in word_positions if wp.get('source') == 'opus_corrected')
    unmatched  = sum(1 for wp in word_positions if wp.get('source') == 'unmatched_textract')
    dropped    = sum(1 for wp in word_positions if wp.get('source') == 'dropped_noise')
    bleed      = sum(1 for wp in word_positions if wp.get('source') == 'dropped_bleedthrough')

    # Zone-level stats
    header_corrected = sum(1 for wp in word_positions
                           if wp.get('zone') == 'header' and wp.get('source') == 'opus_corrected')
    header_unmatched = sum(1 for wp in word_positions
                           if wp.get('zone') == 'header' and wp.get('source') == 'unmatched_textract')

    print(
        f"[OK] Alignment page {page_number}: "
        f"{matched_lines}/{len(body_opus_lines)} body lines matched  "
        f"{confirmed} confirmed  {aligned} aligned  {corrected} corrected  "
        f"{unmatched} unmatched_textract  {dropped} noise  {bleed} bleedthrough  "
        f"{unmatched_opus} opus-only lines"
    )
    if has_header_zone:
        print(
            f"[INFO] Header zone: {header_corrected} opus_corrected, "
            f"{header_unmatched} unmatched_textract"
        )

    final_transcription = opus_text.strip()
    return word_positions, final_transcription


# ─── Handler ──────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    output_s3_uri      = force_string(event.get('outputS3Uri'))
    word_positions_uri = force_string(event.get('wordPositionsS3Uri'))
    image_key          = force_string(event.get('imageKey'))
    page_key           = force_string(event.get('pageKey'))
    letter_id          = force_string(event.get('letterId'))

    if not output_s3_uri or not word_positions_uri or not image_key \
            or not page_key or not letter_id:
        raise ValueError(
            f"Missing required input. "
            f"outputS3Uri={output_s3_uri}, wordPositionsS3Uri={word_positions_uri}, "
            f"imageKey={image_key}, pageKey={page_key}, letterId={letter_id}"
        )

    page_number = page_number_from_key(page_key)

    bucket, key = parse_s3_uri(output_s3_uri)
    raw = json.loads(s3.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8'))

    thinking_block, tool_result = extract_thinking_and_tool_result(
        raw, expected_tool_name="submit_transcription"
    )
    transcription_text = force_string(tool_result.get('transcriptionText', ''))

    if not transcription_text:
        raise ValueError(f"Empty transcriptionText in tool result for {image_key}")

    has_sig = bool(thinking_block and thinking_block.get('signature'))
    print(
        f"[OK] {letter_id}/{page_key} — {len(transcription_text)} chars transcribed, "
        f"thinking={'present, signature=' + str(has_sig) if thinking_block else 'absent'}"
    )

    wp_bucket, wp_key = parse_s3_uri(word_positions_uri)
    wp_raw = json.loads(
        s3.get_object(Bucket=wp_bucket, Key=wp_key)['Body'].read().decode('utf-8')
    )

    # Parse new format: {words, layoutZones, headerColumns, pageLines} or legacy flat array
    if isinstance(wp_raw, dict) and 'words' in wp_raw:
        word_positions   = wp_raw['words']
        layout_zones     = wp_raw.get('layoutZones', [])
        header_columns   = wp_raw.get('headerColumns', [])
        page_lines       = wp_raw.get('pageLines', [])
        print(f"[OK] Word positions loaded with layout zones: "
              f"{len(layout_zones)} zones, {len(header_columns)} header columns, "
              f"{len(page_lines)} page lines")
    else:
        # Legacy format: flat array of word positions
        word_positions   = wp_raw if isinstance(wp_raw, list) else []
        layout_zones     = []
        header_columns   = []
        page_lines       = []
        print(f"[OK] Word positions loaded (legacy format, no layout zones)")

    bleed_count = sum(1 for wp in word_positions if wp.get('source') == 'dropped_bleedthrough')
    print(
        f"[OK] {len(word_positions)} word positions "
        f"({len(word_positions) - bleed_count} active, {bleed_count} bleed-through skipped)"
    )

    word_positions, final_transcription = align_opus_to_positions(
        transcription_text, word_positions, page_number,
        layout_zones=layout_zones,
        header_columns=header_columns,
        page_lines=page_lines
    )

    # Write back in the same wrapped format
    wp_output = {
        "words":         word_positions,
        "layoutZones":   layout_zones,
        "headerColumns": header_columns,
        "pageLines":     page_lines,
    }
    s3.put_object(
        Bucket=wp_bucket, Key=wp_key,
        Body=json.dumps(wp_output, separators=(',', ':')),
        ContentType="application/json"
    )
    print(f"[OK] Updated word positions → {word_positions_uri}")

    return {
        "imageKey":          image_key,
        "pageKey":           page_key,
        "pageNumber":        page_number,
        "letterId":          letter_id,
        "transcriptionText": final_transcription,
        "wordPositions":     word_positions,
        "thinkingBlock":     thinking_block or {},
        "thinkingText":      (thinking_block or {}).get('thinking', ''),
    }
