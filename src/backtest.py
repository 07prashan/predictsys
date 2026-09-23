"""Walk-forward backtest: one or more real models vs a naive baseline vs the
bookmaker's own odds."""

import pandas as pd
from sklearn.base import clone
from sklearn.metrics import accuracy_score, log_loss

OUTCOMES = ["A", "D", "H"]  # sorted lexicographically, to match sklearn's expected label order
OUTCOME_CODES = {outcome: i for i, outcome in enumerate(OUTCOMES)}  # some classifiers (XGBoost) need numeric y


def decay_weights(dates: pd.Series, as_of, half_life_days: float) -> "pd.Series[float]":
    """A training match halves in influence every `half_life_days` before `as_of`.
    Shared between the backtest loop and predict.py's live fit, so a model tuned
    with a given half-life is actually fit the same way when used for real -
    the backtest's own sample weighting was previously never applied to the live
    model at all, which would have made the tuned decay setting a no-op in practice."""
    days_before = (as_of - dates).dt.days.clip(lower=0)
    return 0.5 ** (days_before / half_life_days)


def _market_probs(df: pd.DataFrame) -> pd.DataFrame | None:
    """Bookmaker-implied probabilities from Bet365 odds, with the overround removed."""
    cols = ["B365H", "B365D", "B365A"]
    if not all(c in df.columns for c in cols) or df[cols].isna().all(axis=None):
        return None
    odds = df[cols].astype(float)
    inv = 1.0 / odds
    probs = inv.div(inv.sum(axis=1), axis=0)
    probs.columns = ["H", "D", "A"]  # matches the B365H/B365D/B365A column order above
    return probs[OUTCOMES]


def walk_forward_backtest(
    matches: pd.DataFrame,
    models: dict,
    season_col: str = "season",
    return_proba: bool = False,
    decay_half_life_days: float = None,
):
    """
    models: {name: (unfitted sklearn-compatible estimator, [feature columns])}.
    Each estimator needs .fit(X, y) / .predict_proba(X). Targets are passed in
    as numeric codes (0/1/2 for A/D/H) rather than the raw strings, since not
    every classifier - XGBoost included - accepts arbitrary string labels; the
    resulting probability columns are then relabelled back to A/D/H positionally.

    Every model is refit from scratch on all seasons strictly before the test
    season, evaluated on that season, then the window rolls forward - nothing
    is ever trained on data from its own test season or later.

    decay_half_life_days: if set, every model is fit with sample_weight decaying
    by this half-life (needs a "Date" column) - a training match halves in
    influence every this-many days before the test period starts. A match from
    four seasons ago probably says less about current form than one from last
    month; None (the default) weighs every training match identically.

    With return_proba=True, also returns {name: proba_df} with every test-fold
    prediction (for "elo"/"gbm"/... plus "naive" and "market"), indexed like
    `matches` - this is what lets an ensemble average several models' predictions.
    """
    seasons = sorted(matches[season_col].unique())
    rows = []
    proba_by_model = {name: [] for name in list(models) + ["naive", "market"]}

    for i in range(1, len(seasons)):
        train = matches[matches[season_col].isin(seasons[:i])]
        test = matches[matches[season_col] == seasons[i]]
        if train.empty or test.empty:
            continue

        row = {"season": seasons[i], "n_matches": len(test)}
        y_train = train["FTR"].map(OUTCOME_CODES)

        sample_weight = None
        if decay_half_life_days is not None:
            sample_weight = decay_weights(train["Date"], test["Date"].min(), decay_half_life_days).to_numpy()

        for name, (estimator, feature_cols) in models.items():
            clf = clone(estimator)
            if sample_weight is not None:
                clf.fit(train[feature_cols], y_train, sample_weight=sample_weight)
            else:
                clf.fit(train[feature_cols], y_train)
            proba = pd.DataFrame(clf.predict_proba(test[feature_cols]), columns=OUTCOMES, index=test.index)
            row[f"{name}_accuracy"] = accuracy_score(test["FTR"], proba.idxmax(axis=1))
            row[f"{name}_logloss"] = log_loss(test["FTR"], proba, labels=OUTCOMES)
            proba_by_model[name].append(proba)

        freqs = train["FTR"].value_counts(normalize=True)
        naive_proba = pd.DataFrame(
            [[freqs.get(o, 0.0) for o in OUTCOMES]] * len(test), columns=OUTCOMES, index=test.index
        )
        row["naive_accuracy"] = accuracy_score(test["FTR"], naive_proba.idxmax(axis=1))
        row["naive_logloss"] = log_loss(test["FTR"], naive_proba, labels=OUTCOMES)
        proba_by_model["naive"].append(naive_proba)

        row["market_accuracy"] = None
        row["market_logloss"] = None
        market_proba = _market_probs(test)
        if market_proba is not None:
            valid = market_proba.dropna()
            row["market_accuracy"] = accuracy_score(test.loc[valid.index, "FTR"], valid.idxmax(axis=1))
            row["market_logloss"] = log_loss(test.loc[valid.index, "FTR"], valid, labels=OUTCOMES)
            proba_by_model["market"].append(market_proba)

        rows.append(row)

    results = pd.DataFrame(rows)
    if not return_proba:
        return results
    proba = {name: pd.concat(frames) for name, frames in proba_by_model.items() if frames}
    return results, proba
