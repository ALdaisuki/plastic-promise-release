//! Integration tests — end-to-end storage + domain pipeline.

use context_engine_core::context_engine::ContextEngine;
use context_engine_core::domain::Tier;
use context_engine_core::memory_worth::MemoryRecord;
use context_engine_core::retrieval::fusion::{weighted_rrf_fuse, WrrfConfig};
use context_engine_core::storage::sqlite_impl::SqliteStorage;
use context_engine_core::storage::{ListFilter, StorageBackend, UpdateFields};
use pyo3::types::PyDict;
use pyo3::{PyObject, Python};
use serde_json::Value;
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

fn create_snapshot_db(
    db_path: &Path,
    canonical_memory_type: &str,
    include_control_table: bool,
    reserve_candidate: bool,
) {
    let conn = rusqlite::Connection::open(db_path).unwrap();
    conn.execute_batch("CREATE TABLE memories (id TEXT PRIMARY KEY, memory_type TEXT);")
        .unwrap();
    conn.execute(
        "INSERT INTO memories (id, memory_type) VALUES (?1, ?2)",
        rusqlite::params!["candidate", canonical_memory_type],
    )
    .unwrap();
    if include_control_table {
        conn.execute_batch("CREATE TABLE synthesis_artifacts (memory_id TEXT PRIMARY KEY);")
            .unwrap();
        if reserve_candidate {
            conn.execute(
                "INSERT INTO synthesis_artifacts (memory_id) VALUES (?1)",
                rusqlite::params!["candidate"],
            )
            .unwrap();
        }
    }
}

fn snapshot_memory(py: Python<'_>, id: &str, memory_type: &str, content: Option<&str>) -> PyObject {
    let dict = PyDict::new(py);
    dict.set_item("id", id).unwrap();
    dict.set_item("memory_type", memory_type).unwrap();
    if let Some(content) = content {
        dict.set_item("content", content).unwrap();
        dict.set_item("source", "codex").unwrap();
        dict.set_item("scope", "global").unwrap();
    }
    dict.into()
}

fn pack_ids(pack: &context_engine_core::context_engine::ContextPack) -> Vec<String> {
    pack.core
        .iter()
        .chain(pack.related.iter())
        .chain(pack.divergent.iter())
        .map(|item| item.id.clone())
        .collect()
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
            format!("m{}", i),
            format!("text {}", i),
            "experience".into(),
            "user".into(),
        );
        if i < 2 {
            r.tier = Tier::Core.as_str().into();
        } else {
            r.tier = Tier::Working.as_str().into();
        }
        db.store(&r).unwrap();
    }
    let filter = ListFilter {
        tier: Some(Tier::Core),
        limit: 10,
        ..Default::default()
    };
    let core_mems = db.list(&filter).unwrap();
    assert_eq!(core_mems.len(), 2);
}

#[test]
fn test_update_and_retrieve() {
    let mut db = SqliteStorage::open(":memory:").unwrap();
    let r = context_engine_core::memory_worth::MemoryRecord::new(
        "u1".into(),
        "original".into(),
        "fact".into(),
        "user".into(),
    );
    db.store(&r).unwrap();
    let updates = UpdateFields {
        category: Some("preference".into()),
        ..Default::default()
    };
    db.update("u1", &updates).unwrap();
    let retrieved = db.get("u1").unwrap().unwrap();
    assert_eq!(retrieved.category, "preference");
    assert_eq!(retrieved.content, "original"); // unchanged
}

#[test]
fn test_weibull_decay() {
    use chrono::{Duration, Utc};
    use context_engine_core::domain::decay::WeibullDecay;
    use context_engine_core::domain::DecayModel;

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
        "w1".into(),
        "test".into(),
        "task".into(),
        "system".into(),
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
        let engine =
            ContextEngine::new_with_backends(db_path_string.clone(), "unused-lancedb".into())
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
    let mut engine =
        ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap();
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
        let engine =
            ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap();
        let pack = engine
            .supply(
                "generate code with context supply contracts".to_string(),
                vec![0.1; 1024],
                "code_generation".to_string(),
                "global".to_string(),
                Vec::new(),
                None,
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
fn test_rust_supply_accepts_two_channel_wrrf_and_reports_rankings() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        let engine =
            ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap();
        let config = serde_json::json!({
            "k": 2,
            "channels": ["vector", "bm25"],
            "weights": {"vector": 0.6, "bm25": 0.4},
            "windows": {"vector": 20, "bm25": 20}
        })
        .to_string();
        let pack = engine
            .supply(
                "weighted reciprocal rank fusion".into(),
                vec![0.1; 1024],
                "debugging".into(),
                "global".into(),
                vec![snapshot_memory(
                    py,
                    "candidate",
                    "experience",
                    Some("weighted reciprocal rank fusion candidate"),
                )],
                Some(config),
            )
            .unwrap();

        let audit = pack
            .audit_metadata
            .get("retrieval_fusion_json")
            .expect("Rust supply must expose versioned fusion audit");
        assert!(audit.contains("weighted-rrf-v1"));
        assert!(pack.channel_rankings.contains_key("vector"));
        assert!(pack.channel_rankings.contains_key("bm25"));
        assert_eq!(
            pack.channel_states["bm25"]["participating"],
            "true"
        );
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

        let engine =
            ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap();
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
                None,
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

        let engine =
            ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap();
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
                None,
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
        assert_eq!(
            pack.pipeline_stats.get("bm25_count"),
            Some(&"1".to_string())
        );
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

        let engine =
            ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap();
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
                None,
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

#[test]
fn test_pyo3_does_not_export_raw_storage_methods() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        let engine = pyo3::Py::new(
            py,
            ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap(),
        )
        .unwrap();
        let instance = engine.as_ref(py);

        for method in [
            "store_memory",
            "get_memory",
            "update_memory",
            "delete_memory",
            "list_memories",
            "memory_stats_json",
        ] {
            assert!(
                !instance.hasattr(method).unwrap(),
                "raw storage method {method} must not be Python-visible"
            );
        }
        assert!(instance.hasattr("supply").unwrap());
    });
}

#[test]
fn test_new_rejects_missing_configured_file_instead_of_falling_back() {
    let db_path = unique_sqlite_path("new_rejects_missing_configured_file");
    cleanup_sqlite_files(&db_path);
    let previous = std::env::var_os("PLASTIC_DB_PATH");
    std::env::set_var("PLASTIC_DB_PATH", &db_path);
    let result = ContextEngine::new();
    match previous {
        Some(value) => std::env::set_var("PLASTIC_DB_PATH", value),
        None => std::env::remove_var("PLASTIC_DB_PATH"),
    }

    assert!(
        result.is_err(),
        "a configured file-backed engine must not silently use :memory:"
    );
}

#[test]
fn test_direct_supply_rejects_synthesis_before_reading_content() {
    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        let engine =
            ContextEngine::new_with_backends(":memory:".into(), "unused-lancedb".into()).unwrap();
        // No `content` or `_vector`: success proves the type gate ran first.
        let pack = engine
            .supply(
                "governance retrieval".into(),
                vec![0.1; 1024],
                "debugging".into(),
                "global".into(),
                vec![snapshot_memory(py, "governed", "synthesis", None)],
                None,
            )
            .unwrap();

        assert!(!pack_ids(&pack).contains(&"governed".to_string()));
        assert!(pack.per_item_stats.iter().any(|row| {
            row.get("id") == Some(&"governed".to_string())
                && row.get("filter_reason") == Some(&"governed_synthesis".to_string())
        }));
    });
}

#[test]
fn test_file_backed_supply_rejects_control_only_governed_id_before_content() {
    let db_path = unique_sqlite_path("file_backed_control_only_gate");
    cleanup_sqlite_files(&db_path);
    create_snapshot_db(&db_path, "experience", true, true);
    let db_path_string = db_path.to_string_lossy().into_owned();

    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        let engine =
            ContextEngine::new_with_backends(db_path_string, "unused-lancedb".into()).unwrap();
        // The Python snapshot lies by omission; canonical control ownership
        // must reject it before content is requested.
        let pack = engine
            .supply(
                "governance retrieval".into(),
                vec![0.1; 1024],
                "debugging".into(),
                "global".into(),
                vec![snapshot_memory(py, "candidate", "experience", None)],
                None,
            )
            .unwrap();

        assert!(!pack_ids(&pack).contains(&"candidate".to_string()));
        assert!(pack.per_item_stats.iter().any(|row| {
            row.get("id") == Some(&"candidate".to_string())
                && row.get("filter_reason") == Some(&"canonical_admission_rejected".to_string())
        }));
    });

    cleanup_sqlite_files(&db_path);
}

#[test]
fn test_rust_storage_reads_python_schema_by_explicit_column_name() {
    let db_path = unique_sqlite_path("explicit_python_schema_projection");
    cleanup_sqlite_files(&db_path);
    let db_path_string = db_path.to_string_lossy().into_owned();

    {
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        // Deliberately use Python's column order, which differs from the old
        // Rust table order. An implicit SELECT * would map this incorrectly.
        conn.execute_batch(
            "CREATE TABLE memories (\
                id TEXT PRIMARY KEY, content TEXT, memory_type TEXT, source TEXT, owner TEXT,\
                tier TEXT, scope TEXT, category TEXT, importance REAL, entity_ids TEXT,\
                created_at TEXT, access_count INTEGER, worth_success INTEGER,\
                worth_failure INTEGER, activation_weight REAL, last_accessed TEXT,\
                tags TEXT, domain TEXT, decay_multiplier REAL, effective_half_life REAL,\
                metadata_json TEXT\
             );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO memories (\
                id, content, memory_type, source, tier, scope, category, importance,\
                created_at, access_count, worth_success, worth_failure, last_accessed,\
                tags, domain, decay_multiplier, effective_half_life, metadata_json\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18)",
            rusqlite::params![
                "python-row",
                "explicit projection is schema-order independent",
                "experience",
                "codex",
                "L2",
                "project:test",
                "decision",
                0.8_f64,
                "2026-07-11T00:00:00+00:00",
                4_i64,
                3_i64,
                1_i64,
                "2026-07-11T01:00:00+00:00",
                "[\"rust\"]",
                "building",
                1.2_f64,
                5.0_f64,
                "{\"origin\":\"python\"}",
            ],
        )
        .unwrap();
    }

    {
        let engine =
            ContextEngine::new_with_backends(db_path_string, "unused-lancedb".into()).unwrap();
        let record = engine.get_memory("python-row".into()).unwrap().unwrap();
        assert_eq!(
            record.content,
            "explicit projection is schema-order independent"
        );
        assert_eq!(record.tier, "L2");
        assert_eq!(record.scope, "project:test");
        assert_eq!(record.category, "decision");
        assert_eq!(record.tags, vec!["rust".to_string()]);
        assert_eq!(record.metadata_json, "{\"origin\":\"python\"}");
    }

    cleanup_sqlite_files(&db_path);
}

#[test]
fn test_file_backed_supply_fails_closed_when_control_schema_is_missing() {
    let db_path = unique_sqlite_path("file_backed_missing_control_schema");
    cleanup_sqlite_files(&db_path);
    create_snapshot_db(&db_path, "experience", false, false);
    let db_path_string = db_path.to_string_lossy().into_owned();

    pyo3::prepare_freethreaded_python();
    Python::with_gil(|py| {
        let engine =
            ContextEngine::new_with_backends(db_path_string, "unused-lancedb".into()).unwrap();
        let result = engine.supply(
            "governance retrieval".into(),
            vec![0.1; 1024],
            "debugging".into(),
            "global".into(),
            vec![snapshot_memory(py, "candidate", "experience", None)],
            None,
        );
        assert!(
            result.is_err(),
            "missing synthesis_artifacts must be unknown canonical state"
        );
    });

    cleanup_sqlite_files(&db_path);
}

fn wrrf_golden_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../tests/fixtures/recall_quality/wrrf-v1-golden.json"
    ))
    .unwrap()
}

fn golden_f64(value: &Value) -> f64 {
    match value {
        Value::Number(number) => number.as_f64().unwrap(),
        Value::String(token) if token == "NaN" => f64::NAN,
        Value::String(token) if token == "Infinity" => f64::INFINITY,
        Value::String(token) if token == "-Infinity" => f64::NEG_INFINITY,
        _ => panic!("unsupported golden float token: {value}"),
    }
}

fn golden_wrrf_config(case: &Value) -> WrrfConfig {
    let config = &case["config"];
    WrrfConfig {
        k: config["k"].as_u64().unwrap() as u32,
        channels: config["channels"]
            .as_array()
            .unwrap()
            .iter()
            .map(|value| value.as_str().unwrap().to_string())
            .collect(),
        weights: config["weights"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(channel, value)| (channel.clone(), golden_f64(value)))
            .collect(),
        windows: config["windows"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(channel, value)| (channel.clone(), value.as_u64().unwrap() as usize))
            .collect(),
    }
}

fn golden_wrrf_rankings(case: &Value) -> Vec<(String, Vec<(String, f64)>)> {
    let config = &case["config"];
    let rankings = case["rankings"].as_object().unwrap();
    config["channels"]
        .as_array()
        .unwrap()
        .iter()
        .map(|channel| {
            let channel = channel.as_str().unwrap();
            let rows = rankings[channel]
                .as_array()
                .unwrap()
                .iter()
                .map(|row| {
                    let row = row.as_array().unwrap();
                    (row[0].as_str().unwrap().to_string(), golden_f64(&row[1]))
                })
                .collect();
            (channel.to_string(), rows)
        })
        .collect()
}

#[test]
fn test_weighted_rrf_matches_shared_golden_fixture() {
    let fixture = wrrf_golden_fixture();
    assert_eq!(fixture["schema_version"], "wrrf-golden/v1");
    let tolerance = fixture["score_tolerance"].as_f64().unwrap();

    for case in fixture["valid_cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let result = weighted_rrf_fuse(&golden_wrrf_rankings(case), &golden_wrrf_config(case))
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        let expected = case["expected"].as_array().unwrap();

        assert_eq!(result.len(), expected.len(), "{name}");
        for (actual, expected) in result.iter().zip(expected) {
            let expected = expected.as_array().unwrap();
            assert_eq!(actual.0, expected[0].as_str().unwrap(), "{name}");
            assert!(
                (actual.1 - golden_f64(&expected[1])).abs() <= tolerance,
                "{name}: actual={} expected={}",
                actual.1,
                golden_f64(&expected[1])
            );
        }
    }
}

#[test]
fn test_weighted_rrf_rejects_representable_shared_invalid_cases() {
    let fixture = wrrf_golden_fixture();
    let pyo3_only_k_cases = ["fractional_k", "boolean_k", "negative_k", "u32_overflow_k"];

    for case in fixture["invalid_cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        if pyo3_only_k_cases.contains(&name) {
            continue;
        }
        let error = weighted_rrf_fuse(&golden_wrrf_rankings(case), &golden_wrrf_config(case))
            .expect_err(name);
        assert_eq!(error, case["expected_error"].as_str().unwrap(), "{name}");
    }
}
