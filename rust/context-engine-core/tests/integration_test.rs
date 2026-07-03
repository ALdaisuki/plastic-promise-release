//! Integration tests — end-to-end storage + domain pipeline.

use context_engine_core::storage::sqlite_impl::SqliteStorage;
use context_engine_core::storage::{ListFilter, StorageBackend, UpdateFields};
use context_engine_core::domain::Tier;

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
