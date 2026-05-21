#!/usr/bin/env python3
"""Backtest the short-term fund signal with historical NAV data.

The backtest uses only information available before each signal date:
- 20/60 day momentum
- trailing 60-day win rate
- trailing 120-day volatility and drawdown

It then measures forward 5/10/20 NAV-point returns and max drawdown.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
LIVE_DATA = ROOT / "data" / "industry-live.json"
OUTPUT = ROOT / "data" / "short-term-backtest.json"


def clean_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def max_drawdown_from_series(close: pd.Series) -> float:
    close = pd.to_numeric(close, errors="coerce").dropna()
    if len(close) < 2:
        return 0.0
    running_high = close.cummax()
    return abs(float(((close / running_high) - 1).min() * 100))


def period_return(close: pd.Series, index: int, sessions: int) -> float:
    if index < sessions:
        return 0.0
    current = float(close.iloc[index])
    previous = float(close.iloc[index - sessions])
    if previous == 0:
        return 0.0
    return (current / previous - 1) * 100


def forward_return(close: pd.Series, index: int, sessions: int) -> float | None:
    if index + sessions >= len(close):
        return None
    current = float(close.iloc[index])
    future = float(close.iloc[index + sessions])
    if current == 0:
        return None
    return (future / current - 1) * 100


def signal_score(close: pd.Series, index: int) -> dict[str, float]:
    returns = close.pct_change().dropna()
    trailing_returns = returns.iloc[max(0, index - 120):index]
    trailing_close = close.iloc[max(0, index - 120): index + 1]
    momentum20 = period_return(close, index, 20)
    momentum60 = period_return(close, index, 60)
    win_rate = float((returns.iloc[max(0, index - 60):index] > 0).mean() * 100)
    volatility = float(trailing_returns.std() * math.sqrt(252) * 100) if len(trailing_returns) > 10 else 100
    drawdown = max_drawdown_from_series(trailing_close)
    risk_score = min(100, volatility * 0.85 + drawdown * 1.35 + max(0, -momentum60) * 0.9)
    score = momentum20 * 1.35 + momentum60 * 0.45 + (win_rate - 50) * 0.55 - risk_score * 0.38 - drawdown * 0.42
    return {
        "score": round(score, 2),
        "momentum20": round(momentum20, 2),
        "momentum60": round(momentum60, 2),
        "winRate": round(win_rate, 1),
        "riskScore": round(risk_score, 1),
        "drawdown": round(drawdown, 2),
    }


def fetch_nav(code: str) -> pd.DataFrame:
    last_error = None
    for attempt in range(3):
        try:
            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            if not df.empty:
                df = df.rename(columns={"净值日期": "date", "单位净值": "nav"})
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
                return df.dropna(subset=["date", "nav"]).sort_values("date").reset_index(drop=True)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch nav failed for {code}: {last_error}")


def backtest_fund(code: str, name: str) -> dict[str, Any]:
    nav = fetch_nav(code)
    close = nav["nav"]
    signals = []
    for index in range(140, len(close) - 21, 5):
        metrics = signal_score(close, index)
        if metrics["score"] < 18 or metrics["momentum20"] <= 0 or metrics["winRate"] < 52 or metrics["riskScore"] > 65:
            continue
        row = {
            "date": nav["date"].iloc[index].date().isoformat(),
            **metrics,
            "forward5": forward_return(close, index, 5),
            "forward10": forward_return(close, index, 10),
            "forward20": forward_return(close, index, 20),
            "forwardDrawdown20": max_drawdown_from_series(close.iloc[index: index + 21]),
        }
        signals.append(row)

    return summarize_fund(code, name, signals)


def summarize_fund(code: str, name: str, signals: list[dict[str, Any]]) -> dict[str, Any]:
    def values(key: str) -> list[float]:
        return [clean_number(signal.get(key)) for signal in signals if signal.get(key) is not None]

    forward5 = values("forward5")
    forward10 = values("forward10")
    forward20 = values("forward20")
    drawdowns = values("forwardDrawdown20")
    return {
        "code": code,
        "name": name,
        "signals": len(signals),
        "winRate5": round(sum(value > 0 for value in forward5) / len(forward5) * 100, 1) if forward5 else 0,
        "winRate10": round(sum(value > 0 for value in forward10) / len(forward10) * 100, 1) if forward10 else 0,
        "winRate20": round(sum(value > 0 for value in forward20) / len(forward20) * 100, 1) if forward20 else 0,
        "avgReturn5": round(sum(forward5) / len(forward5), 2) if forward5 else 0,
        "avgReturn10": round(sum(forward10) / len(forward10), 2) if forward10 else 0,
        "avgReturn20": round(sum(forward20) / len(forward20), 2) if forward20 else 0,
        "avgDrawdown20": round(sum(drawdowns) / len(drawdowns), 2) if drawdowns else 0,
        "latestSignals": signals[-5:],
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def main() -> None:
    with LIVE_DATA.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    fund_map = {}
    for industry in data.get("industries", []):
        for fund in industry.get("candidateFunds", []):
            fund_map[fund["code"]] = fund["name"]

    results = []
    errors = []
    for code, name in fund_map.items():
        try:
            results.append(backtest_fund(code, name))
        except Exception as exc:  # noqa: BLE001
            errors.append({"code": code, "name": name, "error": str(exc)})

    ranked = sorted(
        results,
        key=lambda item: item["winRate10"] * 0.35 + item["avgReturn10"] * 8 + item["signals"] * 0.4 - item["avgDrawdown20"] * 2,
        reverse=True,
    )
    payload = {
        "asOf": pd.Timestamp.today().date().isoformat(),
        "model": "short-term-backtest-v1",
        "fundCount": len(fund_map),
        "results": ranked,
        "errors": errors,
    }
    write_json_atomic(OUTPUT, payload)
    print(f"wrote {OUTPUT} with {len(results)} funds, {len(errors)} errors")


if __name__ == "__main__":
    main()

