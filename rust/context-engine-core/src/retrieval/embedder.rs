//! Embedder trait — defined here, implemented in Python.
//!
//! Rust never calls this directly. Vectors arrive via context_engine::supply()
//! parameter `task_vector: Vec<f32>`.

/// Trait for text-to-vector embedding.
/// Implementation lives in Python (using openai SDK).
/// Rust side: vectors injected via supply(task_vector) parameter.
pub trait Embedder {
    /// Embed a single text into a vector of dimension `dim()`.
    fn embed(&self, text: &str) -> Result<Vec<f32>, String>;
    /// Embed multiple texts in batch.
    fn embed_batch(&self, texts: &[String]) -> Result<Vec<Vec<f32>>, String>;
    /// Return the embedding dimension (e.g., 1536 for OpenAI text-embedding-3-small).
    fn dim(&self) -> usize;
}
