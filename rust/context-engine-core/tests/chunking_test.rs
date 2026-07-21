use context_engine_core::chunking::{
    structure_aware_chunks, structure_chunk_projection, STRUCTURE_CHUNK_SCHEMA_VERSION,
};

fn char_slice(source: &str, start: usize, end: usize) -> String {
    source.chars().skip(start).take(end - start).collect()
}

fn source_body(text: &str, has_heading_context: bool) -> &str {
    if has_heading_context {
        text.split_once('\n').map_or(text, |(_, body)| body)
    } else {
        text
    }
}

#[test]
fn structure_v1_preserves_unicode_structure_and_fenced_code() {
    let source = concat!(
        "# \u{8bb0}\u{5fc6}\n\n",
        "\u{7b2c}\u{4e00}\u{6bb5}\u{3002}\n\n",
        "## \u{68c0}\u{7d22}\n\n",
        "- keep heading\n",
        "- keep code\n\n",
        "```python\n",
        "value = 1\n",
        "# not a heading\n",
        "```\n\n",
        "tail",
    );

    let chunks = structure_aware_chunks(source, 256, Some(256), None);

    assert_eq!(
        chunks
            .iter()
            .map(|chunk| chunk.kind.as_str())
            .collect::<Vec<_>>(),
        vec!["paragraph", "list", "code", "paragraph"]
    );
    assert_eq!(chunks[0].heading_path, vec!["\u{8bb0}\u{5fc6}"]);
    assert_eq!(
        chunks[1].heading_path,
        vec!["\u{8bb0}\u{5fc6}", "\u{68c0}\u{7d22}"]
    );
    assert!(chunks[2].text.contains("# not a heading"));
    for chunk in &chunks {
        assert_eq!(
            char_slice(source, chunk.source_start, chunk.source_end),
            source_body(&chunk.text, !chunk.heading_path.is_empty())
        );
    }
}

#[test]
fn structure_v1_uses_unicode_character_offsets() {
    let source = "# A\r\n## B\r\n\u{4e2d}\u{6587}\u{1f642} text";
    let chunks = structure_aware_chunks(source, 128, Some(128), None);

    assert_eq!(chunks.len(), 2);
    assert_eq!(chunks[0].kind, "heading");
    assert_eq!(chunks[1].kind, "paragraph");
    assert_eq!(
        char_slice(source, chunks[1].source_start, chunks[1].source_end),
        "\u{4e2d}\u{6587}\u{1f642} text"
    );
    assert_eq!(chunks[1].source_end, source.chars().count());
}

#[test]
fn structure_v1_bounded_projection_retains_tail() {
    let source = (0..8)
        .map(|index| format!("# S{index}\n\nBody {index}"))
        .collect::<Vec<_>>()
        .join("\n\n");
    let chunks = structure_aware_chunks(&source, 16, Some(32), Some(3));

    assert_eq!(chunks.len(), 3);
    assert!(chunks[0].source_start < chunks[1].source_start);
    assert!(chunks[1].source_start < chunks[2].source_start);
    assert!(chunks[2].text.contains("Body 7"));
    assert_eq!(chunks[2].source_end, source.chars().count());
}

#[test]
fn structure_v1_projection_has_stable_identity() {
    let source = "# Memory\n\nContent.";
    let first = structure_chunk_projection(source, 64, Some(64), None);
    let second = structure_chunk_projection(source, 64, Some(64), None);

    assert_eq!(first, second);
    assert_eq!(first.len(), 1);
    assert_eq!(first[0].schema_version, STRUCTURE_CHUNK_SCHEMA_VERSION);
    assert_eq!(first[0].ordinal, 0);
    assert_eq!(first[0].heading_path, vec!["Memory"]);
    assert!(first[0].chunk_id.starts_with("chunk_"));
    assert_eq!(first[0].chunk_id.len(), 26);
    assert_eq!(first[0].source_hash.len(), 64);
    assert_eq!(first[0].text_hash.len(), 64);
}

#[test]
fn structure_v1_empty_input_is_addressable() {
    let rows = structure_chunk_projection("", 64, Some(64), None);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].kind, "empty");
    assert_eq!(rows[0].source_start, 0);
    assert_eq!(rows[0].source_end, 0);
}

#[test]
fn structure_v1_treats_empty_atx_markers_as_paragraph_material() {
    let source = "# \n?";
    let chunks = structure_aware_chunks(source, 64, Some(64), None);

    assert_eq!(chunks.len(), 1);
    assert_eq!(chunks[0].kind, "paragraph");
    assert!(chunks[0].heading_path.is_empty());
    assert_eq!(chunks[0].source_start, 0);
    assert_eq!(chunks[0].source_end, source.chars().count());
}

#[test]
fn structure_v1_accepts_whitespace_after_list_markers() {
    for source in ["*\tx", "-\tx", "+\tx", "1.\tx", "1)\tx"] {
        let chunks = structure_aware_chunks(source, 64, Some(64), None);
        assert_eq!(chunks.len(), 1, "source={source:?}");
        assert_eq!(chunks[0].kind, "list", "source={source:?}");
    }
}
