#!/usr/bin/env python3
"""Evaluate recorded fund purchases against the captured recommendation signal."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PURCHASES = ROOT / "data" / "purchases.json"
OUTPUT = ROOT / "data" / "purchase-evaluation.json"
EVALUATION_POINTS = 10
MIN_OPTIMIZATION_SAMPLE = 10


def read_purchases() -> list[dict[str, Any]]:
    if not PURCHASES.exists():
        return []
    with PURCHASES.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("purchases", []) if isinstance(payload, dict) else []


def fetch_nav(code: str) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            frame = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            if not frame.empty:
                frame = frame.rename(columns={"净值日期": "date", "单位净值": "nav"})
                frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
                frame["nav"] = pd.to_numeric(frame["nav"], errors="coerce")
                return frame.dropna(subset=["date", "nav"]).sort_values("date").reset_index(drop=True)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch nav failed for {code}: {last_error}")


def percent_return(entry_nav: float, current_nav: float) -> float:
    if entry_nav == 0:
        return 0.0
    return (current_nav / entry_nav - 1) * 100


def evaluate_purchase(purchase: dict[str, Any], nav: pd.DataFrame) -> dict[str, Any]:
    buy_date = pd.Timestamp(str(purchase["buyDate"]))
    entry_rows = nav[nav["date"] >= buy_date]
    if entry_rows.empty:
        return {**purchase, "status": "pending-nav", "successEligible": False}
    entry_index = int(entry_rows.index[0])
    entry = nav.iloc[entry_index]
    latest = nav.iloc[-1]
    amount = float(purchase.get("amount", 0))
    current_return = percent_return(float(entry["nav"]), float(latest["nav"]))
    result = {
        **purchase,
        "status": "active",
        "entryNavDate": entry["date"].date().isoformat(),
        "entryNav": round(float(entry["nav"]), 4),
        "latestNavDate": latest["date"].date().isoformat(),
        "latestNav": round(float(latest["nav"]), 4),
        "currentReturn": round(current_return, 2),
        "currentProfit": round(amount * current_return / 100, 2),
        "successEligible": False,
    }
    snapshot = purchase.get("strategySnapshot", {})
    if not snapshot.get("tracked"):
        result["strategyStatus"] = "untracked"
        return result
    if snapshot.get("recordedDate") != purchase.get("buyDate"):
        result["strategyStatus"] = "historical-no-signal"
        return result
    future_index = entry_index + EVALUATION_POINTS
    if future_index >= len(nav):
        result["strategyStatus"] = "pending"
        return result
    future = nav.iloc[future_index]
    forward_return = percent_return(float(entry["nav"]), float(future["nav"]))
    result.update(
        {
            "strategyStatus": "evaluated",
            "successEligible": True,
            "evaluationDate": future["date"].date().isoformat(),
            "forwardReturn10": round(forward_return, 2),
            "strategySuccess": forward_return > 0,
        }
    )
    return result


def optimization_summary(evaluated: list[dict[str, Any]]) -> dict[str, Any]:
    if len(evaluated) < MIN_OPTIMIZATION_SAMPLE:
        return {
            "status": "collecting",
            "minimumSample": MIN_OPTIMIZATION_SAMPLE,
            "message": f"策略样本不足 {MIN_OPTIMIZATION_SAMPLE} 笔，暂不调整筛选阈值。",
        }
    low_risk = [row for row in evaluated if float(row.get("strategySnapshot", {}).get("riskScore", 100)) <= 55]
    high_probability = [
        row for row in evaluated if float(row.get("strategySnapshot", {}).get("upsideProbability", 0)) >= 65
    ]

    def win_rate(rows: list[dict[str, Any]]) -> float | None:
        return round(sum(bool(row.get("strategySuccess")) for row in rows) / len(rows) * 100, 1) if rows else None

    all_win_rate = win_rate(evaluated) or 0
    low_risk_win_rate = win_rate(low_risk)
    high_probability_win_rate = win_rate(high_probability)
    suggestions = []
    if low_risk_win_rate is not None and len(low_risk) >= 5 and low_risk_win_rate >= all_win_rate + 8:
        suggestions.append("低风险样本胜率更好，可考虑收紧风险分上限至 55。")
    if high_probability_win_rate is not None and len(high_probability) >= 5 and high_probability_win_rate >= all_win_rate + 8:
        suggestions.append("高上涨评分样本胜率更好，可优先观察上涨评分不低于 65 的基金。")
    if not suggestions:
        suggestions.append("目前分组优势不显著，建议保持现有阈值并继续累计样本。")
    return {
        "status": "ready",
        "minimumSample": MIN_OPTIMIZATION_SAMPLE,
        "lowRiskCount": len(low_risk),
        "lowRiskWinRate": low_risk_win_rate,
        "highProbabilityCount": len(high_probability),
        "highProbabilityWinRate": high_probability_win_rate,
        "suggestions": suggestions,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, path)


def main() -> None:
    purchases = read_purchases()
    nav_cache: dict[str, pd.DataFrame] = {}
    results = []
    errors = []
    for purchase in purchases:
        code = str(purchase.get("code", "")).zfill(6)
        try:
            if code not in nav_cache:
                nav_cache[code] = fetch_nav(code)
            results.append(evaluate_purchase(purchase, nav_cache[code]))
        except Exception as exc:  # noqa: BLE001
            errors.append({"id": purchase.get("id"), "code": code, "error": str(exc)})

    evaluated = [row for row in results if row.get("successEligible")]
    invested = sum(float(row.get("amount", 0)) for row in results)
    profit = sum(float(row.get("currentProfit", 0)) for row in results)
    payload = {
        "asOf": date.today().isoformat(),
        "evaluationPoints": EVALUATION_POINTS,
        "purchaseCount": len(purchases),
        "trackedCount": sum(bool(row.get("strategySnapshot", {}).get("tracked")) for row in results),
        "evaluatedCount": len(evaluated),
        "successCount": sum(bool(row.get("strategySuccess")) for row in evaluated),
        "successRate": round(sum(bool(row.get("strategySuccess")) for row in evaluated) / len(evaluated) * 100, 1)
        if evaluated
        else None,
        "investedAmount": round(invested, 2),
        "currentProfit": round(profit, 2),
        "currentReturn": round(profit / invested * 100, 2) if invested else None,
        "purchases": sorted(results, key=lambda row: str(row.get("buyDate", "")), reverse=True),
        "optimization": optimization_summary(evaluated),
        "errors": errors,
    }
    write_json_atomic(OUTPUT, payload)
    print(f"wrote {OUTPUT} with {len(results)} purchases, {len(evaluated)} evaluated signals")


if __name__ == "__main__":
    main()
