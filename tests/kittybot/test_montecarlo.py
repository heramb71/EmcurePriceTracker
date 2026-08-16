"""Monte Carlo resampling on synthetic win/loss outcome series."""
import numpy as np
import pytest

from src.kittybot import montecarlo


def _outcomes(n: int, win_pct: float, seed: int = 0) -> list[bool]:
    rng = np.random.default_rng(seed)
    return [bool(x) for x in rng.random(n) < win_pct]


# ── resample_paths ──────────────────────────────────────────────────────────

def test_resample_paths_is_deterministic_under_a_fixed_seed():
    outcomes = _outcomes(100, 0.5, seed=1)
    a = montecarlo.resample_paths(outcomes, n_paths=200, trades_per_path=50, seed=42)
    b = montecarlo.resample_paths(outcomes, n_paths=200, trades_per_path=50, seed=42)
    assert np.array_equal(a, b)


def test_resample_paths_shape_includes_starting_equity_column():
    outcomes = _outcomes(50, 0.5, seed=2)
    paths = montecarlo.resample_paths(outcomes, n_paths=10, trades_per_path=30, seed=1)
    assert paths.shape == (10, 31)
    assert np.all(paths[:, 0] == 1.0)


def test_resample_paths_raises_on_empty_outcomes():
    with pytest.raises(ValueError):
        montecarlo.resample_paths([], n_paths=10)


def test_resample_paths_defaults_trades_per_path_to_len_outcomes():
    outcomes = _outcomes(37, 0.6, seed=3)
    paths = montecarlo.resample_paths(outcomes, n_paths=5, seed=1)
    assert paths.shape == (5, 38)


# ── path_max_drawdowns ──────────────────────────────────────────────────────

def test_path_max_drawdowns_on_a_known_curve():
    # equity: 1.0 -> 1.5 (peak) -> 0.75 (50% drawdown from peak) -> 0.9
    paths = np.array([[1.0, 1.5, 0.75, 0.9]])
    dd = montecarlo.path_max_drawdowns(paths)
    assert dd[0] == pytest.approx(50.0)


def test_path_max_drawdowns_is_zero_for_a_monotonically_rising_curve():
    paths = np.array([[1.0, 1.1, 1.2, 1.3]])
    dd = montecarlo.path_max_drawdowns(paths)
    assert dd[0] == pytest.approx(0.0)


# ── ruin_probability ─────────────────────────────────────────────────────────

def test_ruin_probability_is_zero_when_every_trade_wins():
    outcomes = [True] * 100
    paths = montecarlo.resample_paths(outcomes, n_paths=500, trades_per_path=50,
                                      win_r=2.0, loss_r=-1.0, risk_pct=1.0, seed=7)
    assert montecarlo.ruin_probability(paths, ruin_threshold_pct=50.0) == 0.0


def test_ruin_probability_is_high_for_a_pathological_all_losing_high_risk_series():
    outcomes = [False] * 100
    paths = montecarlo.resample_paths(outcomes, n_paths=500, trades_per_path=50,
                                      win_r=2.0, loss_r=-1.0, risk_pct=10.0, seed=7)
    assert montecarlo.ruin_probability(paths, ruin_threshold_pct=50.0) > 90.0


# ── summarize ────────────────────────────────────────────────────────────────

def test_summarize_percentiles_are_ordered():
    outcomes = _outcomes(150, 0.55, seed=11)
    result = montecarlo.summarize(outcomes, n_paths=2000, trades_per_path=100, seed=5)
    assert result.pctile_10_return_pct <= result.pctile_50_return_pct <= result.pctile_90_return_pct


def test_summarize_verdict_passes_when_ruin_probability_within_bound():
    outcomes = [True] * 100
    result = montecarlo.summarize(outcomes, n_paths=500, trades_per_path=50, seed=9)
    assert result.ruin_probability_pct == 0.0
    assert "within the" in result.verdict


def test_summarize_verdict_fails_when_ruin_probability_above_bound():
    outcomes = [False] * 100
    result = montecarlo.summarize(outcomes, n_paths=500, trades_per_path=50,
                                  risk_pct=10.0, seed=9)
    assert result.ruin_probability_pct > 5.0
    assert "ABOVE the" in result.verdict


def test_summarize_reports_requested_trades_per_path_and_n_paths():
    outcomes = _outcomes(80, 0.5, seed=13)
    result = montecarlo.summarize(outcomes, n_paths=300, trades_per_path=40, seed=2)
    assert result.n_paths == 300
    assert result.trades_per_path == 40
