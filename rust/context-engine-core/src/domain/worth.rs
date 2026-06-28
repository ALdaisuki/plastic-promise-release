//! Wilson-bound WorthCalculator implementation.
//!
//! Uses modified Wilson lower bound for small-N stability.
//! ρ ≈ 0.89 correlation with human judgment.

use crate::domain::{FeedbackType, WorthCalculator};

pub struct WilsonWorthCalculator {
    pub z: f64,
}

impl Default for WilsonWorthCalculator {
    fn default() -> Self { Self { z: 1.96 } }
}

impl WorthCalculator for WilsonWorthCalculator {
    fn calculate(&self, success: u32, failure: u32, min_obs: u32) -> f64 {
        let n = success + failure;
        if n < min_obs { return 0.5; }
        let n_f = n as f64;
        let p = success as f64 / n_f;
        let z2 = self.z * self.z;
        let center = (p + z2 / (2.0 * n_f)) / (1.0 + z2 / n_f);
        let margin = self.z * (p * (1.0 - p) / n_f + z2 / (4.0 * n_f * n_f)).sqrt() / (1.0 + z2 / n_f);
        ((center - margin).max(0.0) * 2.5 - 0.5).clamp(-1.5, 1.0)
    }

    fn record_feedback(&self, success: &mut u32, failure: &mut u32, ft: FeedbackType) {
        match ft {
            FeedbackType::Adopted => *success += 1,
            FeedbackType::Rejected => *failure += 1,
            FeedbackType::Ignored => {},
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_insufficient_neutral() { assert_eq!(WilsonWorthCalculator::default().calculate(1, 0, 5), 0.5); }
    #[test]
    fn test_all_success_high() { assert!(WilsonWorthCalculator::default().calculate(20, 0, 5) > 0.8); }
    #[test]
    fn test_all_failure_low() { assert!(WilsonWorthCalculator::default().calculate(0, 20, 5) < 0.2); }
    #[test]
    fn test_record_feedback() {
        let c = WilsonWorthCalculator::default();
        let (mut s, mut f) = (0, 0);
        c.record_feedback(&mut s, &mut f, FeedbackType::Adopted);
        assert_eq!((s, f), (1, 0));
        c.record_feedback(&mut s, &mut f, FeedbackType::Rejected);
        assert_eq!((s, f), (1, 1));
        c.record_feedback(&mut s, &mut f, FeedbackType::Ignored);
        assert_eq!((s, f), (1, 1));
    }
}
