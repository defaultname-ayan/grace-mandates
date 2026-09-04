"""Logistic regression in pure Python (spec 7).

Deliberately small and inspectable, and deliberately not sklearn: the point of
this component is a calibrated gate on whether the expensive adjudicator runs
at all, not a modelling contribution. Weights are persisted so a reviewer can
read them.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Sequence


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60.0)))
    e = math.exp(max(z, -60.0))
    return e / (1.0 + e)


class LogisticRisk:
    def __init__(self, names: Sequence[str]):
        self.names = list(names)
        self.w = [0.0] * len(self.names)
        self.b = 0.0
        self.mu = [0.0] * len(self.names)
        self.sd = [1.0] * len(self.names)
        self.trained = False
        self.train_stats: dict = {}
        #: Risk level at which a pre-emptive pause is worth its cost. Selected
        #: on the training split only (see predict/train.tune_threshold).
        self.preemptive_threshold: float = 0.60

    # ------------------------------------------------------------ training
    def fit(
        self,
        X: list[list[float]],
        y: list[int],
        *,
        epochs: int = 600,
        lr: float = 0.35,
        l2: float = 1e-3,
        class_weight: bool = False,
    ) -> LogisticRisk:
        """Unweighted by default.

        Class weighting improves ranking on an imbalanced cohort but pushes
        predicted probabilities toward 0.5, which destroys calibration -- and a
        miscalibrated p_fail is useless as an action threshold. Measured: with
        weighting, Brier 0.156 against 0.108 for simply predicting the base
        rate. Unweighted logistic regression is calibrated, and the action
        threshold is chosen separately on the training split.
        """
        n, d = len(X), len(self.names)
        if n == 0:
            return self
        # standardise; a constant column gets sd=1 so it contributes only bias
        for j in range(d):
            col = [row[j] for row in X]
            mu = sum(col) / n
            var = sum((v - mu) ** 2 for v in col) / max(1, n - 1)
            self.mu[j] = mu
            self.sd[j] = math.sqrt(var) if var > 1e-12 else 1.0
        Z = [[(row[j] - self.mu[j]) / self.sd[j] for j in range(d)] for row in X]

        pos = sum(y)
        if class_weight:
            w_pos = n / (2.0 * pos) if pos else 1.0
            w_neg = n / (2.0 * (n - pos)) if (n - pos) else 1.0
        else:
            w_pos = w_neg = 1.0

        self.w = [0.0] * d
        self.b = 0.0
        for _ in range(epochs):
            gw = [0.0] * d
            gb = 0.0
            for i in range(n):
                p = sigmoid(sum(self.w[j] * Z[i][j] for j in range(d)) + self.b)
                wt = w_pos if y[i] else w_neg
                err = wt * (p - y[i])
                gb += err
                zi = Z[i]
                for j in range(d):
                    gw[j] += err * zi[j]
            self.b -= lr * gb / n
            for j in range(d):
                self.w[j] -= lr * (gw[j] / n + l2 * self.w[j])
        self.trained = True
        self.train_stats = {
            "n": n, "positives": pos, "base_rate": round(pos / n, 4),
            "epochs": epochs, "lr": lr, "l2": l2, "class_weight": class_weight,
        }
        return self

    # ---------------------------------------------------------- prediction
    def predict(self, x: Sequence[float]) -> float:
        z = sum(
            self.w[j] * ((x[j] - self.mu[j]) / self.sd[j]) for j in range(len(self.names))
        ) + self.b
        return sigmoid(z)

    def top_weights(self, k: int = 8) -> list[tuple[str, float]]:
        return sorted(zip(self.names, self.w, strict=True), key=lambda t: -abs(t[1]))[:k]

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {
            "names": self.names, "w": self.w, "b": self.b, "mu": self.mu, "sd": self.sd,
            "trained": self.trained, "train_stats": self.train_stats,
            "preemptive_threshold": self.preemptive_threshold,
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> LogisticRisk:
        raw = json.loads(Path(path).read_text())
        m = cls(raw["names"])
        m.w, m.b, m.mu, m.sd = raw["w"], raw["b"], raw["mu"], raw["sd"]
        m.trained = raw.get("trained", True)
        m.train_stats = raw.get("train_stats", {})
        m.preemptive_threshold = raw.get("preemptive_threshold", 0.60)
        return m


def brier(probs: Sequence[float], labels: Sequence[int]) -> float:
    if not probs:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(probs, labels, strict=True)) / len(probs)


def calibration_table(probs: Sequence[float], labels: Sequence[int], bins: int = 5) -> list[dict]:
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(probs) if (lo <= p < hi or (b == bins - 1 and p == 1.0))]
        if not idx:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": 0, "predicted": None, "actual": None})
            continue
        out.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": len(idx),
            "predicted": round(sum(probs[i] for i in idx) / len(idx), 4),
            "actual": round(sum(labels[i] for i in idx) / len(idx), 4),
        })
    return out
