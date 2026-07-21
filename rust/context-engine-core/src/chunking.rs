use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const STRUCTURE_CHUNK_SCHEMA_VERSION: &str = "structure-v1";

#[derive(Debug, Clone, PartialEq, Eq)]
struct StructuralBlock {
    kind: String,
    text: String,
    heading_path: Vec<String>,
    start: usize,
    end: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChunkMaterial {
    pub text: String,
    pub kind: String,
    pub heading_path: Vec<String>,
    pub source_start: usize,
    pub source_end: usize,
    pub context_truncated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChunkProjection {
    pub schema_version: String,
    pub chunk_id: String,
    pub ordinal: usize,
    pub text: String,
    pub kind: String,
    pub heading_path: Vec<String>,
    pub source_start: usize,
    pub source_end: usize,
    pub source_hash: String,
    pub text_hash: String,
    pub context_truncated: bool,
}

pub fn structure_aware_chunks(
    text: &str,
    target_chars: usize,
    hard_chars: Option<usize>,
    max_chunks: Option<usize>,
) -> Vec<ChunkMaterial> {
    if text.is_empty() {
        return vec![ChunkMaterial {
            text: String::new(),
            kind: "empty".to_string(),
            heading_path: vec![],
            source_start: 0,
            source_end: 0,
            context_truncated: false,
        }];
    }
    let target = target_chars.max(1);
    let hard = hard_chars.unwrap_or(target).max(target);
    let blocks = parse_structural_blocks(text);
    let materials = pack_blocks(blocks, target, hard);
    limit_chunk_materials(materials, max_chunks)
}

pub fn structure_chunk_projection(
    text: &str,
    target_chars: usize,
    hard_chars: Option<usize>,
    max_chunks: Option<usize>,
) -> Vec<ChunkProjection> {
    let source_hash = sha256_hex(text.as_bytes());
    structure_aware_chunks(text, target_chars, hard_chars, max_chunks)
        .into_iter()
        .enumerate()
        .map(|(ordinal, material)| {
            let text_hash = sha256_hex(material.text.as_bytes());
            let payload = [
                STRUCTURE_CHUNK_SCHEMA_VERSION.to_string(),
                source_hash.clone(),
                ordinal.to_string(),
                material.kind.clone(),
                material.source_start.to_string(),
                material.source_end.to_string(),
                text_hash.clone(),
            ]
            .join("\0");
            ChunkProjection {
                schema_version: STRUCTURE_CHUNK_SCHEMA_VERSION.to_string(),
                chunk_id: format!("chunk_{}", &sha256_hex(payload.as_bytes())[..20]),
                ordinal,
                text: material.text,
                kind: material.kind,
                heading_path: material.heading_path,
                source_start: material.source_start,
                source_end: material.source_end,
                source_hash: source_hash.clone(),
                text_hash,
                context_truncated: material.context_truncated,
            }
        })
        .collect()
}

fn parse_structural_blocks(text: &str) -> Vec<StructuralBlock> {
    let mut blocks = Vec::new();
    let mut heading_stack: Vec<String> = Vec::new();
    let mut pending: Vec<(usize, String)> = Vec::new();
    let mut pending_heading: Option<(usize, String, Vec<String>)> = None;
    let mut offset = 0usize;
    let mut in_fence = false;
    let mut fence_marker = '\0';

    for line in lines_keep_ends(text) {
        let raw = line.trim_end_matches(['\r', '\n']);
        let stripped = raw.trim();
        let fence = fence_marker_for(raw);

        if let Some(marker) = fence {
            if !in_fence {
                pending_heading = None;
                flush_pending(&mut pending, None, &heading_stack, &mut blocks);
                in_fence = true;
                fence_marker = marker;
                pending.push((offset, line.to_string()));
            } else if fence_marker == marker {
                pending.push((offset, line.to_string()));
                flush_pending(&mut pending, Some("code"), &heading_stack, &mut blocks);
                in_fence = false;
                fence_marker = '\0';
            } else {
                pending.push((offset, line.to_string()));
            }
            offset += char_len(line);
            continue;
        }

        if in_fence {
            pending.push((offset, line.to_string()));
            offset += char_len(line);
            continue;
        }

        if let Some((level, heading_text)) = parse_heading(raw) {
            flush_pending(&mut pending, None, &heading_stack, &mut blocks);
            if let Some((start, text, parent_path)) = pending_heading.take() {
                let end = start + char_len(&text);
                blocks.push(StructuralBlock {
                    kind: "heading".to_string(),
                    text,
                    heading_path: parent_path,
                    start,
                    end,
                });
            }
            heading_stack.truncate(level.saturating_sub(1));
            heading_stack.push(heading_text);
            pending_heading = Some((
                offset,
                raw.trim().to_string(),
                heading_stack[..heading_stack.len().saturating_sub(1)].to_vec(),
            ));
            offset += char_len(line);
            continue;
        }

        if stripped.is_empty() {
            flush_pending(&mut pending, None, &heading_stack, &mut blocks);
            offset += char_len(line);
            continue;
        }

        pending_heading = None;
        if !pending.is_empty() && starts_new_atomic_block(&pending, raw) {
            flush_pending(&mut pending, None, &heading_stack, &mut blocks);
        }
        pending.push((offset, line.to_string()));
        offset += char_len(line);
    }

    flush_pending(
        &mut pending,
        if in_fence { Some("code") } else { None },
        &heading_stack,
        &mut blocks,
    );
    if let Some((start, text, parent_path)) = pending_heading {
        let end = start + char_len(&text);
        blocks.push(StructuralBlock {
            kind: "heading".to_string(),
            text,
            heading_path: parent_path,
            start,
            end,
        });
    }
    blocks
}

fn flush_pending(
    pending: &mut Vec<(usize, String)>,
    forced_kind: Option<&str>,
    heading_stack: &[String],
    blocks: &mut Vec<StructuralBlock>,
) {
    if pending.is_empty() {
        return;
    }
    let raw = pending
        .iter()
        .map(|(_, line)| line.as_str())
        .collect::<String>();
    let leading = char_len(&raw) - char_len(raw.trim_start());
    let body = raw.trim().to_string();
    let start = pending[0].0 + leading;
    let end = start + char_len(&body);
    if !body.is_empty() {
        blocks.push(StructuralBlock {
            kind: forced_kind
                .map(str::to_string)
                .unwrap_or_else(|| classify_block(&body).to_string()),
            text: body,
            heading_path: heading_stack.to_vec(),
            start,
            end,
        });
    }
    pending.clear();
}

fn pack_blocks(
    blocks: Vec<StructuralBlock>,
    target_chars: usize,
    hard_chars: usize,
) -> Vec<ChunkMaterial> {
    let mut chunks = Vec::new();
    let mut current: Option<ChunkMaterial> = None;

    for block in blocks {
        for piece in split_oversized_block(block, hard_chars) {
            let contextual = contextual_text(&piece, Some(hard_chars));
            let candidate = ChunkMaterial {
                text: contextual,
                kind: piece.kind.clone(),
                heading_path: piece.heading_path.clone(),
                source_start: piece.start,
                source_end: piece.end,
                context_truncated: contextual_text_truncated(&piece, hard_chars),
            };
            let Some(existing) = current.take() else {
                current = Some(candidate);
                continue;
            };
            let same_context = existing.heading_path == candidate.heading_path;
            let compatible_kind = existing.kind == "paragraph" && candidate.kind == "paragraph";
            let candidate_body = without_heading_context(&candidate.text, &candidate.heading_path);
            let combined_len = char_len(&existing.text) + 2 + char_len(candidate_body);
            if same_context && compatible_kind && combined_len <= target_chars {
                current = Some(ChunkMaterial {
                    text: format!("{}\n\n{}", existing.text, candidate_body),
                    kind: "paragraph".to_string(),
                    heading_path: existing.heading_path,
                    source_start: existing.source_start,
                    source_end: candidate.source_end,
                    context_truncated: existing.context_truncated || candidate.context_truncated,
                });
            } else {
                chunks.push(existing);
                current = Some(candidate);
            }
        }
    }
    if let Some(chunk) = current {
        chunks.push(chunk);
    }
    if chunks.is_empty() {
        vec![ChunkMaterial {
            text: String::new(),
            kind: "empty".to_string(),
            heading_path: vec![],
            source_start: 0,
            source_end: 0,
            context_truncated: false,
        }]
    } else {
        chunks
    }
}

fn split_oversized_block(block: StructuralBlock, hard_chars: usize) -> Vec<StructuralBlock> {
    if char_len(&contextual_text(&block, None)) <= hard_chars {
        return vec![block];
    }
    let chars: Vec<char> = block.text.chars().collect();
    let mut pieces = Vec::new();
    let mut cursor = 0usize;
    while cursor < chars.len() {
        let remaining: String = chars[cursor..].iter().collect();
        let limit = hard_chars
            .saturating_sub(char_len(&heading_prefix(&block.heading_path)) + 1)
            .max(1);
        let end = if char_len(&remaining) <= limit {
            char_len(&remaining)
        } else {
            preferred_break(&remaining, limit)
        };
        let raw_piece: String = chars[cursor..cursor + end].iter().collect();
        let mut piece_text = raw_piece.trim().to_string();
        let actual_end = if piece_text.is_empty() {
            let fallback_end = limit.min(chars.len() - cursor).max(1);
            piece_text = chars[cursor..cursor + fallback_end].iter().collect();
            fallback_end
        } else {
            end
        };
        let leading = char_len(&raw_piece[..]) - char_len(raw_piece.trim_start());
        let start = block.start + cursor + leading;
        let piece_end = start + char_len(&piece_text);
        pieces.push(StructuralBlock {
            kind: block.kind.clone(),
            text: piece_text,
            heading_path: block.heading_path.clone(),
            start,
            end: piece_end,
        });
        cursor += actual_end;
        while cursor < chars.len() && chars[cursor].is_whitespace() {
            cursor += 1;
        }
    }
    pieces
}

fn preferred_break(text: &str, limit: usize) -> usize {
    let chars: Vec<char> = text.chars().take(limit).collect();
    let markers: &[&[char]] = &[
        &['\n'],
        &['\u{3002}'],
        &['\u{ff01}'],
        &['\u{ff1f}'],
        &['\u{ff0c}'],
        &['.', ' '],
        &['!', ' '],
        &['?', ' '],
        &[' '],
    ];
    for marker in markers {
        if let Some(position) = rfind_chars(&chars, marker) {
            if position >= (limit / 3).max(1) {
                return position + marker.len();
            }
        }
    }
    limit
}

fn rfind_chars(haystack: &[char], needle: &[char]) -> Option<usize> {
    if needle.is_empty() || needle.len() > haystack.len() {
        return None;
    }
    (0..=haystack.len() - needle.len())
        .rev()
        .find(|&index| haystack[index..index + needle.len()] == *needle)
}

fn limit_chunk_materials(
    materials: Vec<ChunkMaterial>,
    max_chunks: Option<usize>,
) -> Vec<ChunkMaterial> {
    let Some(max_chunks) = max_chunks else {
        return materials;
    };
    let limit = max_chunks.max(1);
    if materials.len() <= limit {
        return materials;
    }
    if limit == 1 {
        return vec![materials.last().cloned().expect("non-empty materials")];
    }
    let mut bounded = materials[..limit - 1].to_vec();
    bounded.push(materials.last().cloned().expect("non-empty materials"));
    bounded
}

fn lines_keep_ends(text: &str) -> Vec<&str> {
    let mut lines: Vec<&str> = text.split_inclusive('\n').collect();
    if !text.is_empty() && !text.ends_with('\n') && lines.is_empty() {
        lines.push(text);
    }
    lines
}

fn parse_heading(raw: &str) -> Option<(usize, String)> {
    let leading_spaces = raw.chars().take_while(|ch| *ch == ' ').count();
    if leading_spaces > 3 {
        return None;
    }
    let rest: String = raw.chars().skip(leading_spaces).collect();
    let level = rest.chars().take_while(|ch| *ch == '#').count();
    if !(1..=6).contains(&level) {
        return None;
    }
    let after: String = rest.chars().skip(level).collect();
    if !after.chars().next().is_some_and(char::is_whitespace) {
        return None;
    }
    let title = after.trim();
    // Keep the Rust parser aligned with Python's non-empty heading contract.
    // An empty ATX marker is ordinary paragraph material for structure-v1.
    if title.is_empty() {
        return None;
    }
    Some((level, title.to_string()))
}

fn fence_marker_for(raw: &str) -> Option<char> {
    let trimmed = raw.trim_start();
    if trimmed.starts_with("```") {
        Some('`')
    } else if trimmed.starts_with("~~~") {
        Some('~')
    } else {
        None
    }
}

fn starts_new_atomic_block(pending: &[(usize, String)], raw: &str) -> bool {
    let current = pending
        .iter()
        .map(|(_, value)| value.as_str())
        .collect::<String>();
    if current.trim().is_empty() {
        return false;
    }
    let current_kind = classify_block(current.trim());
    let next_kind = classify_block(raw.trim());
    current_kind != next_kind
        && ([current_kind, next_kind].contains(&"table")
            || [current_kind, next_kind].contains(&"list"))
}

fn classify_block(text: &str) -> &'static str {
    let lines: Vec<&str> = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    if lines.is_empty() {
        "empty"
    } else if fence_marker_for(lines[0]).is_some() {
        "code"
    } else if lines.len() >= 2 && lines[0].contains('|') && lines[1].contains('|') {
        "table"
    } else if is_list_line(lines[0]) {
        "list"
    } else {
        "paragraph"
    }
}

fn is_list_line(line: &str) -> bool {
    let trimmed = line.trim_start();
    let mut chars = trimmed.chars();
    if matches!(chars.next(), Some('-' | '*' | '+'))
        && chars.next().is_some_and(char::is_whitespace)
    {
        return true;
    }
    let digits = trimmed.chars().take_while(char::is_ascii_digit).count();
    if digits == 0 {
        return false;
    }
    let mut suffix = trimmed.chars().skip(digits);
    matches!(suffix.next(), Some('.' | ')'))
        && suffix.next().is_some_and(char::is_whitespace)
}

fn heading_prefix(heading_path: &[String]) -> String {
    heading_path.join(" > ")
}

fn contextual_text(block: &StructuralBlock, max_chars: Option<usize>) -> String {
    let mut prefix = heading_prefix(&block.heading_path);
    if let Some(max_chars) = max_chars {
        if !prefix.is_empty() {
            let available = max_chars.saturating_sub(char_len(&block.text) + 1);
            if available < char_len(&prefix) {
                prefix = take_last_chars(&prefix, available);
            }
        }
    }
    if prefix.is_empty() {
        block.text.clone()
    } else {
        format!("{}\n{}", prefix, block.text)
    }
}

fn contextual_text_truncated(block: &StructuralBlock, max_chars: usize) -> bool {
    !heading_prefix(&block.heading_path).is_empty()
        && char_len(&contextual_text(block, None)) > max_chars
}

fn without_heading_context<'a>(text: &'a str, heading_path: &[String]) -> &'a str {
    if heading_path.is_empty() {
        text
    } else {
        text.split_once('\n').map_or(text, |(_, body)| body)
    }
}

fn take_last_chars(text: &str, count: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    chars[chars.len().saturating_sub(count)..].iter().collect()
}

fn char_len(text: &str) -> usize {
    text.chars().count()
}

fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
