//! Integration tests — end-to-end storage + domain pipeline.

use context_engine_core::storage::sqlite_impl::SqliteStorage;
use context_engine_core::storage::{ListFilter, StorageBackend, UpdateFields};
use context_engine_core::domain::Tier;
use context_engine_core::context_engine::ContextEngine;
use context_engine_core::memory_worth::MemoryRecord;
use pyo3::types::PyDict;
use pyo3::{PyObject, Python};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

fn unique_sqlite_path(test_name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    std::env::temp_dir().join(format!(
        "plastic_promise_{}_{}_{}.db",
        test_name,
        std::process::id(),
        nanos
    ))
}

fn cleanup_sqlite_files(db_path: &Path) {
    for path in [
        db_path.to_path_buf(),
        PathBuf::from(format!("{}-wal", db_path.display())),
        PathBuf::from(format!("{}-shm", db_path.display())),
    ] {
        if path.exists() {
            std::fs::remove_file(&path).unwrap();
        }
    }
}

#[test]
fn test_full_store_and_list_cycle() {
    let mut db = SqliteStorage::open(":memory:").unwrap();
    for i in 0..10 {
        let record = context_engine_core::memory_worth::MemoryRecord::new(
            format!("mem-{}", i),
            format!("content number {}", i),
            "experience".into(),
            "user".into(),
        );
        db.store(&record).unwrap();
    }
    assert_eq!(db.total_count().unwrap(), 10);
    let stats = db.stats(None).unwrap();
    assert_eq!(stats.total, 10);
    assert_eq!(stats.healthy, 10);
}

#[test]
fn test_tier_filtering() {
    let mut db = SqliteStorage::open(":memory:").unwrap();
    for i in 0..5 {
        let mut r = context_engine_core::memory_worth::MemoryRecord::new(
            format!("m{}", i), format!("text {}", i),
            "experience".into(), "user".into(),
        );
        if i < 2 {
            r.tier = Tier::Core.as_str().into();
        } else {
            r.tier = Tier::Working.as_str().into();
        }
        db.store(&r).unwrap();
    }
    let filter = ListFilter { tier: Some(Tier::Core), limit: 10, ..Default::default() };
    let core_mems = db.list(&filter).unwrap();
    assert_eq!(core_mems.len(), 2);
}

#[test]
fn test_update_and_retrieve() {
    let mut db = SqliteStorage::open(":memory:").unwrap();
    let r = context_engine_core::memory_worth::MemoryRecord::new(
        "u1".into(), "original".into(), "fact".into(), "user".into(),
    );
    db.store(&r).unwrap();
    let updates = UpdateFields { category: Some("preference".into()), ..Default::default() };
    db.update("u1", &updates).unwrap();
    let retrieved = db.get("u1").unwrap().unwrap();
    assert_eq!(retrieved.category, "preference");
    assert_eq!(retrieved.content, "original"); // unchanged
}

#[test]
fn test_weibull_decay() {
    use context_engine_core::domain::decay::WeibullDecay;
    use context_engine_core::domain::DecayModel;
    use chrono::{Duration, Utc};

    let decay = WeibullDecay::default();
    let created = Utc::now() - Duration::days(14);
    let score = decay.compute(Tier::Working, &created, &created, 0, 0.5);
    assert!(score < 0.2); // 14-day-old working memory should be heavily decayed
}

#[test]
fn test_worth_calculator_end_to_end() {
    use context_engine_core::domain::worth::WilsonWorthCalculator;
    use context_engine_core::domain::WorthCalculator;

    let calc = WilsonWorthCalculator::default();
    // High quality memory
    assert!(calc.calculate(20, 2, 5) > 0.7);
    // Low quality memory
    assert!(calc.calculate(2, 20, 5) < 0.3);
    // Insufficient data
    assert_eq!(calc.calculate(1, 0, 5), 0.5);
}

#[test]
fn test_memory_record_worth_integration() {
    let mut r = context_engine_core::memory_worth::MemoryRecord::new(
        "w1".into(), "test".into(), "task".into(), "system".into(),
    );
    assert_eq!(r.tier, "L1");
    assert_eq!(r.scope, "global");
    assert_eq!(r.category, "other");
    assert_eq!(r.importance, 0.7);

    // Record feedback through the struct methods
    r.record_adopted();
    r.record_adopted();
    r.record_rejected();
    r.record_ignored();
    assert_eq!(r.worth_success, 2);
    assert_eq!(r.worth_failure, 2);
}

#[test]
fn test_new_with_backends_reads_existing_sqlite_path() {
    let db_path = unique_sqlite_path("new_with_backends_reads_existing_sqlite_path");
    cleanup_sqlite_files(&db_path);
    let db_path_string = db_path.to_string_lossy().into_owned();

    {
        let mut db = SqliteStorage::open(&db_path_string).unwrap();
        let record = MemoryRecord::new(
            "backend-row".into(),
            "new_with_backends must read this row from the provided sqlite path".into(),
            "experience".into(),
            "codex".into(),
        );
        db.store(&record).unwrap();
    }

    {
        let engine = ContextEngine::new_with_backends(db_path_string.clone(), "unused-lancedb".into())
            .unwrap();
        let retrieved = engine.get_memory("backend-row".into()).unwrap().unwrap();

        assert_eq!(
            retrieved.content,
            "new_with_backends must read this row from the provided sqlite path"
        );
    }

    cleanup_sqlite_files(&db_path);
}

#[test]
fn test_new_with_backends_missing_sqlite_path_returns_error() {
    let db_path = unique_sqlite_path("new_with_backends_missing_sqlite_path_returns_error");
    let _ = std::fs::remove_file(&db_path);
    let db_path_string = db_path.to_string_lossy().into_owned();

    let result = ContextEngine::new_with_backends(db_path_string, "unused-lancedb".into());

    assert!(
        result.is_err(),
        "missing sqlite path should not silently fall back to :memory:"
    );
}

#[test]
fn test_new_with_backends_memory_sqlite_path_still_works() {
    let mut engine = ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into())
        .unwrap();
    let record = MemoryRecord::new(
        "memory-row".into(),
        ":memory: backend remains writable".into(),
        "experience".into(),
        "codex".into(),
    );

    engine.store_memory(record).unwrap();
    let retrieved = engine.get_memory("memory-row".into()).unwrap().unwrap();

    assert_eq!(retrieved.content, ":memory: backend remains writable");
}

#[test]
fn test_rust_supply_principle_injection_audit_count() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|_| {
        let engine = ContextEngine::new();
        let pack = engine
            .supply(
                "generate code with context supply contracts".to_string(),
                vec![0.1; 1024],
                "code_generation".to_string(),
                "global".to_string(),
                Vec::new(),
            )
            .unwrap();

        assert!(
            !pack.activated_principles.is_empty(),
            "code_generation should activate principles"
        );

        let audit_count = pack
            .audit_metadata
            .get("principle_injection_count")
            .expect("audit metadata should include principle_injection_count")
            .parse::<usize>()
            .unwrap();
        assert_eq!(audit_count, pack.activated_principles.len());
    });
}

#[test]
fn test_audit_telemetry_is_filtered_from_rust_supply() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        fn memory_map(py: Python<'_>, id: &str, content: &str, source: &str) -> PyObject {
            let dict = PyDict::new(py);
            dict.set_item("id", id).unwrap();
            dict.set_item("content", content).unwrap();
            dict.set_item("source", source).unwrap();
            dict.set_item("memory_type", "reflection").unwrap();
            dict.set_item("scope", "global").unwrap();
            dict.set_item("worth_success", 0).unwrap();
            dict.set_item("worth_failure", 0).unwrap();
            dict.into()
        }

        let engine = ContextEngine::new();
        let memories = vec![
            memory_map(
                py,
                "audit",
                "AUDIT trust=0.60 pipeline=0.94 domain=0.80 bridge=1.00 mem_q=0.06 -> 0.68",
                "maintenance_daemon",
            ),
            memory_map(
                py,
                "useful",
                "context engine request isolation fixes memory recall race",
                "codex",
            ),
        ];

        let pack = engine
            .supply(
                "context engine memory recall isolation".to_string(),
                vec![0.1; 1024],
                "debugging".to_string(),
                "global".to_string(),
                memories,
            )
            .unwrap();

        let all_ids: Vec<String> = pack
            .core
            .iter()
            .chain(pack.related.iter())
            .chain(pack.divergent.iter())
            .map(|item| item.id.clone())
            .collect();

        assert!(!all_ids.contains(&"audit".to_string()));
    });
}

#[test]
fn test_audit_telemetry_is_excluded_from_rust_snapshot_indexes() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        fn memory_map(py: Python<'_>, id: &str, content: &str, source: &str) -> PyObject {
            let dict = PyDict::new(py);
            dict.set_item("id", id).unwrap();
            dict.set_item("content", content).unwrap();
            dict.set_item("source", source).unwrap();
            dict.set_item("memory_type", "reflection").unwrap();
            dict.set_item("scope", "global").unwrap();
            dict.set_item("worth_success", 0).unwrap();
            dict.set_item("worth_failure", 0).unwrap();
            dict.into()
        }

        let engine = ContextEngine::new();
        let memories = vec![
            memory_map(
                py,
                "bare_audit",
                "AUDIT trust=0.60 pipeline=0.94 domain=0.80 bridge=1.00 mem_q=0.06 -> 0.68",
                "maintenance_daemon",
            ),
            memory_map(
                py,
                "prefixed_audit",
                "- [0.70] [maintenance_daemon] AUDIT trust=0.60 pipeline=0.94 domain=0.80 bridge=1.00 mem_q=0.06 -> 0.68",
                "maintenance_daemon",
            ),
            memory_map(
                py,
                "useful",
                "context engine request scope memory recall race isolation",
                "codex",
            ),
        ];

        let pack = engine
            .supply(
                "request scope memory recall audit trust".to_string(),
                vec![0.1; 1024],
                "debugging".to_string(),
                "global".to_string(),
                memories,
            )
            .unwrap();

        let all_items = pack
            .core
            .iter()
            .chain(pack.related.iter())
            .chain(pack.divergent.iter());

        for item in all_items {
            assert!(
                !item.content.to_ascii_lowercase().contains("audit trust="),
                "telemetry leaked into recall layer: {}",
                item.content
            );
        }
        assert_eq!(pack.pipeline_stats.get("bm25_count"), Some(&"1".to_string()));
    });
}

#[test]
fn test_project_metadata_filtering_in_rust_snapshot_supply() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        fn memory_map(
            py: Python<'_>,
            id: &str,
            content: &str,
            project_id: &str,
            visibility: &str,
            source_class: &str,
        ) -> PyObject {
            let dict = PyDict::new(py);
            dict.set_item("id", id).unwrap();
            dict.set_item("content", content).unwrap();
            dict.set_item("source", "codex").unwrap();
            dict.set_item("memory_type", "experience").unwrap();
            dict.set_item("scope", "global").unwrap();
            dict.set_item("project_id", project_id).unwrap();
            dict.set_item("visibility", visibility).unwrap();
            dict.set_item("source_class", source_class).unwrap();
            dict.set_item("worth_success", 0).unwrap();
            dict.set_item("worth_failure", 0).unwrap();
            dict.into()
        }

        let engine = ContextEngine::new();
        let memories = vec![
            memory_map(
                py,
                "same_core",
                "billing service release memory api router",
                "project:app",
                "project",
                "experience",
            ),
            memory_map(
                py,
                "other_core",
                "billing service release memory api router",
                "project:other",
                "project",
                "experience",
            ),
            memory_map(
                py,
                "global_core",
                "billing service release memory api router",
                "project:legacy-global",
                "global",
                "experience",
            ),
            memory_map(
                py,
                "telemetry_core",
                "billing service release memory api router",
                "project:app",
                "project",
                "telemetry",
            ),
            memory_map(
                py,
                "prompt_core",
                "billing service release memory api router",
                "project:app",
                "project",
                "prompt",
            ),
            memory_map(
                py,
                "shared_divergent",
                "unrelated inspiration pattern",
                "project:other",
                "shared",
                "experience",
            ),
        ];

        let pack = engine
            .supply_with_project_context(
                "billing service release memory api router".to_string(),
                vec![0.1; 1024],
                "debugging".to_string(),
                "global".to_string(),
                memories,
                "project:app".to_string(),
                "strict".to_string(),
                false,
            )
            .unwrap();

        let all_ids: Vec<String> = pack
            .core
            .iter()
            .chain(pack.related.iter())
            .chain(pack.divergent.iter())
            .map(|item| item.id.clone())
            .collect();

        assert!(all_ids.contains(&"same_core".to_string()));
        assert!(all_ids.contains(&"global_core".to_string()));
        assert!(!all_ids.contains(&"other_core".to_string()));
        assert!(!all_ids.contains(&"telemetry_core".to_string()));
        assert!(!all_ids.contains(&"prompt_core".to_string()));
        assert!(!all_ids.contains(&"shared_divergent".to_string()));
    });
}
