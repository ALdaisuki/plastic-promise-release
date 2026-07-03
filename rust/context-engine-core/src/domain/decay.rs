//! Weibull stretched-exponential decay model.
//!
//! Formula: score_multiplier = exp(-(age_days / lambda)^β) * importance_factor
//! where β varies per tier and λ = half_life / ln(2)^(1/β).
//!
//! Access reinforcement: effective_half_life = base * min(1 + rf * access_count, max_mult).

use crate::domain::{DecayModel, Tier};
use chrono::{DateTime, Utc};

pub struct WeibullDecay {
    pub reinforcement_factor: f64,
    pub max_half_life_multiplier: f64,
}

impl Default for WeibullDecay {
    fn default() -> Self {
        Self {
            reinforcement_factor: 0.5,
            max_half_life_multiplier: 3.0,
        }
    }
}

impl DecayModel for WeibullDecay {
    fn compute(
        &self,
        tier: Tier,
        created_at: &DateTime<Utc>,
        last_accessed: &DateTime<Utc>,
        access_count: u32,
        importance: f64,
    ) -> f64 {
        let now = Utc::now();
        let age_days = (now - *created_at).num_hours() as f64 / 24.0;
        if age_days < 0.0 {
            return 1.0;
        }
        let half_life = self.effective_half_life(
            tier,
            access_count,
            self.reinforcement_factor,
            self.max_half_life_multiplier,
        );
        let beta = tier.decay_beta();
        let lambda = half_life / (2.0_f64.ln().powf(1.0 / beta));
        let decay = (-(age_days / lambda).powf(beta)).exp();
        let importance_factor = 0.85 + 0.15 * importance;
        (decay * importance_factor).clamp(0.0, 1.0)
    }

    fn effective_half_life(&self, tier: Tier, access_count: u32, rf: f64, max_mult: f64) -> f64 {
        tier.base_half_life_days() * (1.0 + rf * access_count as f64).min(max_mult)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    #[test]
    fn test_fresh_no_decay() {
        let d = WeibullDecay::default();
        let now = Utc::now();
        assert!(d.compute(Tier::Working, &now, &now, 0, 0.7) > 0.95);
    }

    #[test]
    fn test_old_decays() {
        let d = WeibullDecay::default();
        let created = Utc::now() - Duration::days(30);
        let accessed = Utc::now() - Duration::days(25);
        assert!(d.compute(Tier::Working, &created, &accessed, 0, 0.5) < 0.2);
    }

    #[test]
    fn test_principle_slow_decay() {
        let d = WeibullDecay::default();
        let created = Utc::now() - Duration::days(365);
        assert!(d.compute(Tier::Principle, &created, &created, 0, 1.0) > 0.5);
    }

    #[test]
    fn test_access_reinforcement() {
        let d = WeibullDecay::default();
        let hl0 = d.effective_half_life(Tier::Core, 0, 0.5, 3.0);
        let hl10 = d.effective_half_life(Tier::Core, 10, 0.5, 3.0);
        assert!(hl10 > hl0 * 2.0);
    }

    #[test]
    fn test_tier_decay_ordering() {
        let d = WeibullDecay::default();
        let created = Utc::now() - Duration::days(7);
        let w = d.compute(Tier::Working, &created, &created, 0, 0.5);
        let r = d.compute(Tier::Recent, &created, &created, 0, 0.5);
        let c = d.compute(Tier::Core, &created, &created, 0, 0.5);
        let p = d.compute(Tier::Principle, &created, &created, 0, 0.5);
        assert!(w < r && r < c && c < p);
    }
}
