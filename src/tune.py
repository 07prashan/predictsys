"""One-off hyperparameter search for Elo, the gradient-boosted model, and the
Poisson model's recency half-life.

Tuning order matters: Elo's own hyperparameters are tuned FIRST, since
elo_diff/abs_elo_diff/elo_momentum all feed into the gradient-boosted model
as features - a better Elo means better inputs downstream. Gradient boosting
is tuned on top of the already-improved Elo. The Poisson model is independent
of both (it only uses goals), so its recency half-life is tuned on its own.

Every search is scored using ONLY seasons before TUNING_CUTOFF (their own
walk-forward folds). Seasons from TUNING_CUTOFF onward are never touched
during any search - they're read exactly once, at the end, with whatever
every search already picked. That's the "hold out a final window and look
at it once" rule from the plan, applied to hyperparameter search instead of
just the model itself.

Usage:
    python src/tune.py
"""

import random

import pandas as pd
from sklearn.linear_model import LogisticRegression
from xgboost import XGBClassifier

from backtest import walk_forward_backtest
from elo import EloConfig
from poisson_model import poisson_walk_forward
from run import ELO_FEATURES, GBM_FEATURES, build_dataset, load_raw_matches, summarize

TUNING_CUTOFF = 2022  # seasons before this pick every hyperparameter; from here on is the held-out read
N_TRIALS = 15

ELO_SEARCH_SPACE = {
    "k_factor": [10, 15, 20, 25, 30, 40],
    "home_advantage": [30, 50, 70, 90, 110],
    "promoted_penalty": [0, 50, 100, 150, 200],
}
GBM_SEARCH_SPACE = {
    "max_depth": [2, 3, 4, 5],
    "n_estimators": [100, 200, 300, 500],
    "learning_rate": [0.02, 0.05, 0.1],
    "min_child_weight": [1, 5, 10, 20],
    "subsample": [0.7, 0.8, 1.0],
    "colsample_bytree": [0.6, 0.8, 1.0],
    "decay_half_life_days": [None, 180, 365, 730, 1460],
}
POISSON_HALF_LIFE_OPTIONS = [None, 365, 730, 1460]


def _make_gbm(params: dict) -> XGBClassifier:
    xgb_params = {k: v for k, v in params.items() if k != "decay_half_life_days"}
    return XGBClassifier(
        objective="multi:softprob", eval_metric="mlogloss", tree_method="hist", enable_categorical=True, **xgb_params
    )


def _weighted_logloss(results: pd.DataFrame, col: str) -> float:
    valid = results.dropna(subset=[col])
    weights = valid["n_matches"]
    return (valid[col] * weights).sum() / weights.sum()


def tune_elo(raw: dict, rng: random.Random) -> EloConfig:
    print(f"=== Tuning Elo: {N_TRIALS} trials, scored only on seasons before {TUNING_CUTOFF} ===\n")
    best_config, best_score = None, float("inf")
    for trial in range(N_TRIALS):
        params = {k: rng.choice(v) for k, v in ELO_SEARCH_SPACE.items()}
        config = EloConfig(**params)
        dataset = build_dataset(elo_config=config, raw=raw, verbose=False)
        tuning_data = dataset[dataset["season"] < TUNING_CUTOFF]
        results = walk_forward_backtest(tuning_data, {"elo": (LogisticRegression(max_iter=1000), ELO_FEATURES)})
        ll = _weighted_logloss(results, "elo_logloss")
        better = ll < best_score
        if better:
            best_score, best_config = ll, config
        print(f"  trial {trial + 1:>2}: log_loss={ll:.4f}  {params}" + ("  <- best so far" if better else ""))
    print(f"\nBest Elo config: {best_config}  (log_loss={best_score:.4f})\n")
    return best_config


def tune_gbm(tuning_data: pd.DataFrame, rng: random.Random) -> dict:
    print(f"=== Tuning gradient boosting: {N_TRIALS} trials, scored only on seasons before {TUNING_CUTOFF} ===\n")
    best_params, best_score = None, float("inf")
    for trial in range(N_TRIALS):
        params = {k: rng.choice(v) for k, v in GBM_SEARCH_SPACE.items()}
        results = walk_forward_backtest(
            tuning_data,
            {"gbm": (_make_gbm(params), GBM_FEATURES)},
            decay_half_life_days=params["decay_half_life_days"],
        )
        ll = _weighted_logloss(results, "gbm_logloss")
        better = ll < best_score
        if better:
            best_score, best_params = ll, params
        print(f"  trial {trial + 1:>2}: log_loss={ll:.4f}  {params}" + ("  <- best so far" if better else ""))
    print(f"\nBest gbm params: {best_params}  (log_loss={best_score:.4f})\n")
    return best_params


def tune_poisson(tuning_data: pd.DataFrame) -> float:
    print(f"=== Tuning Poisson recency half-life, scored only on seasons before {TUNING_CUTOFF} ===\n")
    best_half_life, best_score = None, float("inf")
    for half_life in POISSON_HALF_LIFE_OPTIONS:
        results, _ = poisson_walk_forward(tuning_data, half_life_days=half_life)
        ll = _weighted_logloss(results, "poisson_logloss")
        better = ll < best_score
        if better:
            best_score, best_half_life = ll, half_life
        print(f"  half_life_days={half_life}: log_loss={ll:.4f}" + ("  <- best so far" if better else ""))
    print(f"\nBest Poisson half-life: {best_half_life}  (log_loss={best_score:.4f})\n")
    return best_half_life


def main() -> None:
    pd.set_option("display.float_format", lambda v: f"{v:.3f}")
    rng = random.Random(0)

    print("=== Loading raw data (cached after first run) ===\n")
    raw = load_raw_matches()

    best_elo_config = tune_elo(raw, rng)

    print("=== Rebuilding the dataset with the tuned Elo config ===\n")
    all_matches = build_dataset(elo_config=best_elo_config, raw=raw)
    all_matches["league"] = all_matches["league"].astype("category")
    tuning_data = all_matches[all_matches["season"] < TUNING_CUTOFF]

    best_gbm_params = tune_gbm(tuning_data, rng)
    best_poisson_half_life = tune_poisson(tuning_data)

    print(f"=== Held-out read on seasons {TUNING_CUTOFF} onward (never seen during tuning) ===\n")
    # elo and gbm_tuned are scored in SEPARATE calls, not one shared one, because
    # decay_half_life_days applies to every model passed to a single call - gbm_tuned's
    # tuned decay must never leak onto elo, which was never validated with any decay at all.
    elo_results = walk_forward_backtest(all_matches, {"elo": (LogisticRegression(max_iter=1000), ELO_FEATURES)})
    gbm_results = walk_forward_backtest(
        all_matches,
        {"gbm_tuned": (_make_gbm(best_gbm_params), GBM_FEATURES)},
        decay_half_life_days=best_gbm_params["decay_half_life_days"],
    )
    poisson_results, _ = poisson_walk_forward(all_matches, half_life_days=best_poisson_half_life)
    # naive/market are computed identically inside every walk_forward_backtest call (same
    # underlying data), so gbm_results' copies are dropped here rather than merged in
    # alongside elo_results' - otherwise pandas suffixes both into naive_accuracy_x/_y etc.
    gbm_only = gbm_results.drop(columns=["naive_accuracy", "naive_logloss", "market_accuracy", "market_logloss"])
    combined = elo_results.merge(gbm_only, on=["season", "n_matches"]).merge(
        poisson_results, on=["season", "n_matches"]
    )
    held_out = combined[combined["season"] >= TUNING_CUTOFF]
    print(held_out.to_string(index=False))

    print("\nWeighted average over the held-out seasons only:")
    summary = summarize(held_out, ["elo", "gbm_tuned", "poisson", "naive", "market"])
    for model, (acc, ll) in summary.items():
        if acc is not None:
            print(f"  {model:>10}: accuracy={acc:.3f}  log_loss={ll:.3f}")

    print(f"\nBest Elo config: {best_elo_config}")
    print(f"Best gbm params: {best_gbm_params}")
    print(f"Best Poisson half-life: {best_poisson_half_life}")


if __name__ == "__main__":
    main()
