//! BM25 text retrieval with version-checked lazy refresh.
//!
//! Okapi BM25 (k1=1.2, b=0.75). Builds document frequency table from
//! in-memory documents, refreshed when the SQLite memory_version changes.

use std::collections::{HashMap, HashSet};

const K1: f64 = 1.2;
const B: f64 = 0.75;

/// English stopwords — minimal set for BM25 tokenization.
const EN_STOPWORDS: &[&str] = &[
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need",
    "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "under", "again", "then", "once", "here", "there",
    "when", "where", "why", "how", "all", "both", "each", "few", "more",
    "most", "other", "some", "such", "only", "own", "same", "so", "than",
    "too", "very", "just", "because", "but", "and", "or", "if", "while",
    "about", "not", "this", "that", "these", "those", "it", "its",
];

/// Minimal Porter stemmer for common English suffixes.
fn porter_stem(word: &str) -> String {
    let mut w = word.to_lowercase();
    if w.len() <= 3 {
        return w;
    }
    // Step 1a
    if w.ends_with("sses") {
        w = w[..w.len() - 2].to_string();
    } else if w.ends_with("ies") {
        w = w[..w.len() - 2].to_string();
    } else if !w.ends_with("ss") && w.ends_with('s') {
        w = w[..w.len() - 1].to_string();
    }
    // Step 1b
    if w.ends_with("eed") && w.len() > 4 {
        w = w[..w.len() - 1].to_string();
    } else if w.ends_with("ed") && !w.ends_with("eed") && w.len() > 3 {
        w = w[..w.len() - 2].to_string();
    } else if w.ends_with("ing") && w.len() > 4 {
        w = w[..w.len() - 3].to_string();
    }
    // Step 4 — common suffixes
    let suffixes = [
        "ement", "ment", "ence", "ance", "able", "ible",
        "ment", "ent", "ant", "ism", "ate", "iti", "ous",
        "ive", "ize", "ion", "al", "er", "ic", "ou", "ly",
    ];
    for suffix in &suffixes {
        if w.ends_with(suffix) && w.len() >= suffix.len() + 3 {
            w = w[..w.len() - suffix.len()].to_string();
            break;
        }
    }
    w
}

/// Tokenize text for BM25. CJK→bigram, English→split+stem+stopword filter.
fn tokenize(text: &str) -> Vec<String> {
    if text.trim().is_empty() {
        return vec![];
    }
    // CJK detection: >30% CJK chars → bigram mode
    let cjk_count = text.chars().filter(|c| ('\u{4E00}'..='\u{9FFF}').contains(c)).count();
    let has_cjk = text.len() > 0 && (cjk_count as f64 / text.len() as f64) > 0.3;

    if has_cjk {
        let chars: Vec<char> = text.chars().filter(|c| !c.is_whitespace()).collect();
        chars.windows(2).map(|w| w.iter().collect()).collect()
    } else {
        text.split_whitespace()
            .map(|w| w.trim_matches(|c: char| !c.is_alphanumeric()))
            .filter(|w| w.len() >= 3)
            .map(|w| porter_stem(w))
            .filter(|w| !EN_STOPWORDS.contains(&w.as_str()))
            .collect()
    }
}

/// Compute IDF: idf = log((N - df + 0.5) / (df + 0.5) + 1)
fn compute_idf(doc_freq: &HashMap<String, usize>, total_docs: usize) -> HashMap<String, f64> {
    doc_freq
        .iter()
        .map(|(term, &df)| {
            let idf = ((total_docs as f64 - df as f64 + 0.5) / (df as f64 + 0.5) + 1.0).ln();
            (term.clone(), idf)
        })
        .collect()
}

/// BM25 index with version-tracked lazy refresh.
pub struct Bm25Index {
    doc_freq: HashMap<String, usize>,
    idf: HashMap<String, f64>,
    doc_tokens: HashMap<String, Vec<String>>,
    avg_doc_len: f64,
    total_docs: usize,
    version: u64,
}

impl Bm25Index {
    pub fn new() -> Self {
        Self {
            doc_freq: HashMap::new(),
            idf: HashMap::new(),
            doc_tokens: HashMap::new(),
            avg_doc_len: 0.0,
            total_docs: 0,
            version: 0,
        }
    }

    pub fn version(&self) -> u64 {
        self.version
    }

    /// Rebuild the index from a list of (id, content) pairs.
    pub fn rebuild(&mut self, docs: &[(String, String)], new_version: u64) {
        self.doc_freq.clear();
        self.idf.clear();
        self.doc_tokens.clear();
        self.version = new_version;

        for (id, content) in docs {
            let tokens = tokenize(content);
            if tokens.is_empty() {
                continue;
            }
            let unique: HashSet<&str> = tokens.iter().map(|s| s.as_str()).collect();
            for term in &unique {
                *self.doc_freq.entry(term.to_string()).or_insert(0) += 1;
            }
            self.doc_tokens.insert(id.clone(), tokens);
        }

        self.total_docs = self.doc_tokens.len();
        self.avg_doc_len = if self.total_docs > 0 {
            self.doc_tokens.values().map(|t| t.len()).sum::<usize>() as f64 / self.total_docs as f64
        } else {
            1.0
        };
        self.idf = compute_idf(&self.doc_freq, self.total_docs);
    }

    /// Score a single document against query terms (Okapi BM25).
    pub fn score(&self, query: &str, doc_id: &str) -> f64 {
        let query_terms = tokenize(query);
        if query_terms.is_empty() {
            return 0.0;
        }
        let doc_tokens = match self.doc_tokens.get(doc_id) {
            Some(t) => t,
            None => return 0.0,
        };
        let doc_len = doc_tokens.len() as f64;
        if doc_len < 1.0 {
            return 0.0;
        }

        // Count term frequencies in document
        let mut tf_counts: HashMap<&str, usize> = HashMap::new();
        for t in doc_tokens {
            *tf_counts.entry(t.as_str()).or_insert(0) += 1;
        }

        let mut score = 0.0;
        for term in &query_terms {
            let idf = match self.idf.get(term) {
                Some(v) => *v,
                None => continue,
            };
            let tf = *tf_counts.get(term.as_str()).unwrap_or(&0) as f64;
            if tf < 1.0 {
                continue;
            }
            let num = tf * (K1 + 1.0);
            let denom = tf + K1 * (1.0 - B + B * doc_len / self.avg_doc_len);
            score += idf * num / denom;
        }
        score
    }

    /// Search and return top-k (doc_id, bm25_score) sorted descending.
    pub fn search(&self, query: &str, k: usize) -> Vec<(String, f64)> {
        let mut results: Vec<(String, f64)> = self
            .doc_tokens
            .keys()
            .map(|id| {
                let raw = self.score(query, id);
                // Sigmoid normalize to [0,1] with temperature 3
                let norm = 1.0 / (1.0 + (-raw / 3.0).exp());
                (id.clone(), norm)
            })
            .filter(|(_, s)| *s > 0.0)
            .collect();
        results.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        results.truncate(k);
        results
    }

    pub fn total_docs(&self) -> usize {
        self.total_docs
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tokenize_english() {
        let tokens = tokenize("running code scanner reviews");
        assert!(!tokens.is_empty());
        // "running" → stem "run"
        assert!(tokens.contains(&"run".to_string()) || tokens.contains(&"running".to_string()));
    }

    #[test]
    fn test_tokenize_empty() {
        assert!(tokenize("").is_empty());
        assert!(tokenize("   ").is_empty());
    }

    #[test]
    fn test_bm25_index_basic() {
        let mut idx = Bm25Index::new();
        let docs = vec![
            ("d1".to_string(), "code review scanner data quality".to_string()),
            ("d2".to_string(), "rust engine implementation plan".to_string()),
            ("d3".to_string(), "code review pipeline fix for scanner".to_string()),
        ];
        idx.rebuild(&docs, 1);

        let results = idx.search("code review scanner", 3);
        assert!(!results.is_empty());
        // d1 and d3 should rank higher than d2
        let top_id = &results[0].0;
        assert!(top_id == "d1" || top_id == "d3");
    }

    #[test]
    fn test_version_tracking() {
        let mut idx = Bm25Index::new();
        assert_eq!(idx.version(), 0);
        idx.rebuild(&[("d1".to_string(), "hello world".to_string())], 42);
        assert_eq!(idx.version(), 42);
    }
}
