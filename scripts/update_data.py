#!/usr/bin/env python3
"""Fetch free public A-share industry and fund data for the dashboard.

Data sources:
- AkShare wrappers over Eastmoney public pages for industry boards, constituents,
  historical board prices, and open fund rankings.

The script writes data/industry-live.json atomically. If a network source fails,
the previous live file is left untouched.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import akshare as ak
import pandas as pd
import py_mini_racer
import requests


FUND_FILTERS = {
    "minScaleYi": 0.5,
    "maxScaleYi": 300.0,
    "minAgeDays": 180,
    "maxTop10HoldingPct": 70.0,
    "maxSingleHoldingPct": 15.0,
    "maxTrackingError": 20.0,
    "minManagerYears": 1.0,
}

FUND_PROFILE_CACHE: dict[str, dict[str, Any]] = {}


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "industry-live.json"

SOURCE_NOTE = "AkShare 1.18.63；底层为东方财富公开行情与基金排行页面"
FUND_REFRESH_NOTE = "个基量化预测已刷新"
CACHE_NOTE = "行业数据沿用上次成功缓存"

THEME_RULES = [
    ("半导体", "科技成长", ["半导体", "芯片", "集成电路"]),
    ("通信", "数字经济", ["通信", "5G", "人工智能", "AI"]),
    ("软件", "数字经济", ["软件", "计算机", "人工智能", "数字经济"]),
    ("互联网", "数字经济", ["互联网", "传媒", "游戏"]),
    ("电池", "先进制造", ["新能源", "电池", "锂电"]),
    ("光伏", "先进制造", ["光伏", "新能源"]),
    ("汽车", "先进制造", ["新能源车", "智能汽车", "汽车"]),
    ("医药", "医药健康", ["医药", "医疗", "创新药"]),
    ("医疗", "医药健康", ["医疗", "医药", "生物"]),
    ("银行", "金融价值", ["银行", "金融", "红利"]),
    ("保险", "金融价值", ["保险", "非银", "金融"]),
    ("证券", "金融价值", ["证券", "金融", "券商"]),
    ("煤炭", "稳健现金流", ["煤炭", "红利", "资源"]),
    ("电力", "稳健现金流", ["电力", "公用事业", "红利"]),
    ("白酒", "内需消费", ["白酒", "消费", "食品饮料"]),
    ("食品", "内需消费", ["食品饮料", "消费"]),
    ("有色", "周期资源", ["有色", "资源", "金属"]),
    ("化工", "周期资源", ["化工", "材料"]),
]


def retry(name: str, func: Callable[[], Any], attempts: int = 3, delay: float = 1.8) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - preserve source failure context
            last_error = exc
            if attempt < attempts:
                time.sleep(delay * attempt)
    raise RuntimeError(f"{name} failed after {attempts} attempts: {last_error}") from last_error


def clean_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def percentile_rank(series: pd.Series, value: float) -> float:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return 50.0
    return round((numeric <= value).mean() * 100, 1)


def theme_for(industry_name: str) -> tuple[str, list[str]]:
    for keyword, theme, fund_keywords in THEME_RULES:
        if keyword in industry_name:
            return theme, fund_keywords
    return "行业轮动", [industry_name]


def period_return(hist: pd.DataFrame, sessions: int) -> float:
    if hist.empty or "收盘" not in hist:
        return 0.0
    close = pd.to_numeric(hist["收盘"], errors="coerce").dropna()
    if close.empty:
        return 0.0
    current = float(close.iloc[-1])
    if len(close) <= sessions:
        previous = float(close.iloc[0])
    else:
        previous = float(close.iloc[-sessions - 1])
    if previous == 0:
        return 0.0
    return round((current / previous - 1) * 100, 2)


def drawdown_score(hist: pd.DataFrame) -> float:
    if hist.empty or "收盘" not in hist:
        return 50.0
    close = pd.to_numeric(hist["收盘"], errors="coerce").dropna().tail(120)
    if len(close) < 2:
        return 50.0
    running_high = close.cummax()
    max_drawdown = ((close / running_high) - 1).min() * 100
    return round(min(100, abs(float(max_drawdown)) * 4), 1)


def series_return(close: pd.Series, sessions: int) -> float:
    close = pd.to_numeric(close, errors="coerce").dropna()
    if close.empty:
        return 0.0
    current = float(close.iloc[-1])
    previous = float(close.iloc[0]) if len(close) <= sessions else float(close.iloc[-sessions - 1])
    if previous == 0:
        return 0.0
    return (current / previous - 1) * 100


def max_drawdown_percent(close: pd.Series, sessions: int = 180) -> float:
    close = pd.to_numeric(close, errors="coerce").dropna().tail(sessions)
    if len(close) < 2:
        return 0.0
    running_high = close.cummax()
    return abs(float(((close / running_high) - 1).min() * 100))


def parse_work_years(value: str) -> float | None:
    if not value:
        return None
    years = 0.0
    if "年" in value:
        try:
            years = float(value.split("年", 1)[0])
        except ValueError:
            years = 0.0
    if "天" in value:
        try:
            day_part = value.split("又")[-1].split("天", 1)[0]
            years += float(day_part) / 365
        except ValueError:
            pass
    return round(years, 2)


def js_date_to_iso(timestamp_ms: Any) -> str | None:
    try:
        return pd.to_datetime(int(timestamp_ms), unit="ms", utc=True).tz_convert("Asia/Shanghai").date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def annual_tracking_error(series_list: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    if len(series_list) < 2:
        return None, None
    fund_data = series_list[0].get("data", [])
    benchmark = choose_tracking_benchmark(series_list)
    if benchmark is None:
        return None, None
    benchmark_name, benchmark_data = benchmark
    if len(fund_data) < 20 or len(benchmark_data) < 20:
        return None, benchmark_name
    fund = pd.DataFrame(fund_data, columns=["date", "return"])
    benchmark = pd.DataFrame(benchmark_data, columns=["date", "return"])
    merged = fund.merge(benchmark, on="date", suffixes=("_fund", "_benchmark"))
    if len(merged) < 20:
        return None, benchmark_name
    fund_level = 1 + pd.to_numeric(merged["return_fund"], errors="coerce") / 100
    benchmark_level = 1 + pd.to_numeric(merged["return_benchmark"], errors="coerce") / 100
    active_return = fund_level.pct_change() - benchmark_level.pct_change()
    active_return = active_return.dropna().tail(120)
    if active_return.empty:
        return None, benchmark_name
    return round(float(active_return.std() * math.sqrt(252) * 100), 2), benchmark_name


def choose_tracking_benchmark(series_list: list[dict[str, Any]]) -> tuple[str, list[Any]] | None:
    candidates = []
    for item in series_list[1:]:
        name = str(item.get("name", ""))
        data = item.get("data", [])
        if not data:
            continue
        score = min(20, len(data) / 10)
        if "同类" in name:
            score -= 50
        if "沪深300" in name:
            score -= 10
        if any(keyword in name for keyword in ["中证", "国证", "创业板", "科创", "红利", "恒生"]):
            score += 40
        candidates.append((score, name, data))
    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0], reverse=True)
    _, name, data = candidates[0]
    return name, data


def latest_holding_concentration(code: str) -> dict[str, Any]:
    current_year = str(date.today().year)
    previous_year = str(date.today().year - 1)
    for year in [current_year, previous_year]:
        try:
            holdings = retry(
                f"fund holding {code} {year}",
                lambda year=year: ak.fund_portfolio_hold_em(symbol=code, date=year),
                attempts=2,
            )
        except Exception:
            continue
        if holdings.empty or "占净值比例" not in holdings:
            continue
        latest_quarter = str(holdings["季度"].iloc[-1]) if "季度" in holdings else year
        latest = holdings[holdings["季度"] == latest_quarter] if "季度" in holdings else holdings
        weights = pd.to_numeric(latest["占净值比例"], errors="coerce").dropna().sort_values(ascending=False)
        if weights.empty:
            continue
        return {
            "report": latest_quarter,
            "top10HoldingPct": round(float(weights.head(10).sum()), 2),
            "largestHoldingPct": round(float(weights.iloc[0]), 2),
            "holdingCount": int(len(weights)),
        }
    return {}


def fund_quality_profile(code: str) -> dict[str, Any]:
    if code in FUND_PROFILE_CACHE:
        return FUND_PROFILE_CACHE[code]

    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    text = retry(
        f"fund profile js {code}",
        lambda: requests.get(url, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=12).text,
        attempts=2,
    )
    ctx = py_mini_racer.MiniRacer()
    ctx.eval(text)

    profile: dict[str, Any] = {
        "scaleYi": None,
        "inceptionDate": None,
        "ageDays": None,
        "managerNames": [],
        "managerMaxYears": None,
        "managerFundSizeYi": None,
        "managerTrackingAbility": None,
        "trackingError": None,
        "trackingBenchmark": None,
        "trackingConfidence": "unknown",
        "top10HoldingPct": None,
        "largestHoldingPct": None,
        "holdingReport": None,
        "qualityScore": 100.0,
        "filterPassed": True,
        "filterIssues": [],
        "missingFields": [],
    }

    try:
        scale = ctx.execute("Data_fluctuationScale")
        series = scale.get("series", [])
        if series:
            profile["scaleYi"] = clean_number(series[-1].get("y"), default=None)
    except Exception:
        pass

    try:
        nav = ctx.execute("Data_netWorthTrend")
        if nav:
            inception = js_date_to_iso(nav[0].get("x"))
            profile["inceptionDate"] = inception
            if inception:
                profile["ageDays"] = (date.today() - date.fromisoformat(inception)).days
    except Exception:
        pass

    try:
        managers = ctx.execute("Data_currentFundManager")
        if managers:
            names = []
            years = []
            fund_sizes = []
            tracking_scores = []
            for manager in managers:
                names.append(str(manager.get("name", "")))
                parsed_years = parse_work_years(str(manager.get("workTime", "")))
                if parsed_years is not None:
                    years.append(parsed_years)
                fund_size = str(manager.get("fundSize", "")).split("亿", 1)[0]
                if fund_size:
                    fund_sizes.append(clean_number(fund_size))
                power = manager.get("power", {})
                categories = power.get("categories", [])
                data = power.get("data", [])
                if "跟踪误差" in categories:
                    index = categories.index("跟踪误差")
                    if index < len(data):
                        tracking_scores.append(clean_number(data[index]))
            profile["managerNames"] = [name for name in names if name]
            profile["managerMaxYears"] = max(years) if years else None
            profile["managerFundSizeYi"] = max(fund_sizes) if fund_sizes else None
            profile["managerTrackingAbility"] = max(tracking_scores) if tracking_scores else None
    except Exception:
        pass

    try:
        tracking_error, benchmark_name = annual_tracking_error(ctx.execute("Data_grandTotal"))
        profile["trackingError"] = tracking_error
        profile["trackingBenchmark"] = benchmark_name
        profile["trackingConfidence"] = "fund-benchmark" if benchmark_name and "沪深300" not in benchmark_name else "broad-index"
    except Exception:
        pass

    concentration = latest_holding_concentration(code)
    if concentration:
        profile["holdingReport"] = concentration.get("report")
        profile["top10HoldingPct"] = concentration.get("top10HoldingPct")
        profile["largestHoldingPct"] = concentration.get("largestHoldingPct")
        profile["holdingCount"] = concentration.get("holdingCount")

    profile = apply_quality_filters(profile)
    FUND_PROFILE_CACHE[code] = profile
    return profile


def apply_quality_filters(profile: dict[str, Any]) -> dict[str, Any]:
    issues = []
    missing = []
    score = 100.0

    checks = [
        ("scaleYi", "规模"),
        ("ageDays", "成立时间"),
        ("managerMaxYears", "基金经理任期"),
        ("top10HoldingPct", "持仓集中度"),
        ("trackingError", "跟踪误差"),
    ]
    for key, label in checks:
        if profile.get(key) is None:
            missing.append(label)
            score -= 5

    scale = profile.get("scaleYi")
    if scale is not None:
        if scale < FUND_FILTERS["minScaleYi"]:
            issues.append(f"规模过小：{scale:.2f}亿")
            score -= 35
        elif scale > FUND_FILTERS["maxScaleYi"]:
            issues.append(f"规模过大：{scale:.2f}亿")
            score -= 12

    age_days = profile.get("ageDays")
    if age_days is not None and age_days < FUND_FILTERS["minAgeDays"]:
        issues.append(f"成立时间不足：{age_days}天")
        score -= 30

    manager_years = profile.get("managerMaxYears")
    if manager_years is not None and manager_years < FUND_FILTERS["minManagerYears"]:
        issues.append(f"基金经理任期偏短：{manager_years:.1f}年")
        score -= 18

    top10 = profile.get("top10HoldingPct")
    if top10 is not None and top10 > FUND_FILTERS["maxTop10HoldingPct"]:
        issues.append(f"前十大持仓集中：{top10:.1f}%")
        score -= (top10 - FUND_FILTERS["maxTop10HoldingPct"]) * 1.2

    largest = profile.get("largestHoldingPct")
    if largest is not None and largest > FUND_FILTERS["maxSingleHoldingPct"]:
        issues.append(f"单一持仓偏高：{largest:.1f}%")
        score -= (largest - FUND_FILTERS["maxSingleHoldingPct"]) * 2

    tracking_error = profile.get("trackingError")
    if tracking_error is not None and tracking_error > FUND_FILTERS["maxTrackingError"]:
        issues.append(f"跟踪误差偏高：{tracking_error:.1f}%")
        score -= (tracking_error - FUND_FILTERS["maxTrackingError"]) * 1.5

    profile["filterIssues"] = issues
    profile["missingFields"] = missing
    profile["filterPassed"] = not issues
    profile["qualityScore"] = round(clamp(score), 1)
    return profile


def fund_quant_metrics(code: str) -> dict[str, Any]:
    nav = retry(
        f"fund nav {code}",
        lambda: ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势"),
        attempts=2,
    )
    if nav.empty or "单位净值" not in nav:
        return {}

    close = pd.to_numeric(nav["单位净值"], errors="coerce").dropna()
    returns = close.pct_change().dropna().tail(180)
    if len(close) < 30 or returns.empty:
        return {}

    momentum_20 = series_return(close, 20)
    momentum_60 = series_return(close, 60)
    momentum_120 = series_return(close, 120)
    volatility = float(returns.tail(120).std() * math.sqrt(252) * 100)
    drawdown = max_drawdown_percent(close)
    win_rate = float((returns.tail(60) > 0).mean() * 100)
    mean_return = float(returns.tail(120).mean() * 252 * 100)
    sharpe_like = mean_return / volatility if volatility > 0 else 0.0
    consistency = clamp(50 + (win_rate - 50) * 1.4 + min(momentum_20, momentum_60) * 0.8)

    upside_probability = clamp(
        50
        + momentum_60 * 0.75
        + momentum_20 * 0.35
        + momentum_120 * 0.18
        + sharpe_like * 7.5
        + (win_rate - 50) * 0.32
        - volatility * 0.22
        - drawdown * 0.28,
        5,
        95,
    )
    risk_score = clamp(volatility * 0.85 + drawdown * 1.35 + max(0, -momentum_60) * 0.9)
    profile = {}
    try:
        profile = fund_quality_profile(code)
    except Exception:
        profile = {"qualityScore": 70.0, "filterPassed": False, "filterIssues": ["详情数据获取失败"], "missingFields": []}
    quality_score = clean_number(profile.get("qualityScore"), default=70.0)
    if not profile.get("filterPassed", False):
        risk_score = clamp(risk_score + 10)
    quant_score = clamp(
        upside_probability * 0.45
        + (100 - risk_score) * 0.25
        + consistency * 0.08
        + quality_score * 0.22
    )

    return {
        "upsideProbability": round(upside_probability, 1),
        "riskScore": round(risk_score, 1),
        "quantScore": round(quant_score, 1),
        "momentum20": round(momentum_20, 2),
        "momentum60": round(momentum_60, 2),
        "momentum120": round(momentum_120, 2),
        "volatility": round(volatility, 2),
        "maxDrawdown": round(drawdown, 2),
        "winRate": round(win_rate, 1),
        "sharpeLike": round(sharpe_like, 2),
        "sampleDays": int(len(returns)),
        "model": "momentum-risk-v1",
        "quality": profile,
    }


def safe_fund_quant_metrics(code: str) -> dict[str, Any]:
    try:
        return fund_quant_metrics(code)
    except Exception:
        return {}


def fund_recommendation(metrics: dict[str, Any]) -> str:
    if not metrics:
        return "数据不足"
    quality = metrics.get("quality", {})
    if quality and not quality.get("filterPassed", True):
        return "过滤淘汰"
    probability = metrics["upsideProbability"]
    risk = metrics["riskScore"]
    if probability >= 65 and risk <= 45:
        return "优先观察"
    if probability >= 58 and risk <= 60:
        return "可小额配置"
    if probability >= 52:
        return "等待回撤"
    return "暂不推荐"


def candidate_funds(fund_rank: pd.DataFrame, keywords: list[str], limit: int = 4) -> list[dict[str, Any]]:
    if fund_rank.empty:
        return []
    name_col = "基金简称"
    code_col = "基金代码"
    frames = []
    for keyword in keywords:
        frames.append(fund_rank[fund_rank[name_col].astype(str).str.contains(keyword, case=False, na=False)])
    matched = pd.concat(frames).drop_duplicates(subset=[code_col]) if frames else pd.DataFrame()
    if matched.empty:
        return []

    for column in ["近1月", "近3月", "近1年", "手续费"]:
        if column not in matched.columns:
            matched[column] = None

    matched = matched.copy()
    matched["近3月数值"] = pd.to_numeric(matched["近3月"], errors="coerce").fillna(-999)
    matched = matched.sort_values("近3月数值", ascending=False).head(limit * 2)
    funds = []
    for _, row in matched.iterrows():
        code = str(row.get(code_col, ""))
        metrics = safe_fund_quant_metrics(code)
        funds.append(
            {
                "code": code,
                "name": str(row.get(name_col, "")),
                "return1m": clean_number(row.get("近1月")),
                "return3m": clean_number(row.get("近3月")),
                "return1y": clean_number(row.get("近1年")),
                "fee": str(row.get("手续费", "")),
                "prediction": metrics,
                "recommendation": fund_recommendation(metrics),
            }
        )
    return sorted(
        funds,
        key=lambda fund: fund.get("prediction", {}).get("quantScore", -1),
        reverse=True,
    )[:limit]


def enrich_fund_predictions(payload: dict[str, Any]) -> dict[str, Any]:
    for industry in payload.get("industries", []):
        funds = industry.get("candidateFunds", [])
        for fund in funds:
            code = str(fund.get("code", ""))
            if not code:
                continue
            metrics = safe_fund_quant_metrics(code)
            fund["prediction"] = metrics
            fund["recommendation"] = fund_recommendation(metrics)
        funds.sort(
            key=lambda fund: fund.get("prediction", {}).get("quantScore", -1),
            reverse=True,
        )
    payload["source"] = append_source_note(payload.get("source", SOURCE_NOTE), FUND_REFRESH_NOTE)
    payload["fundSummary"] = summarize_funds(payload.get("industries", []))
    return payload


def summarize_funds(industries: list[dict[str, Any]]) -> dict[str, Any]:
    funds = [fund for industry in industries for fund in industry.get("candidateFunds", [])]
    total = len(funds)
    passed = 0
    filtered = 0
    insufficient = 0
    issues: dict[str, int] = {}
    for fund in funds:
        prediction = fund.get("prediction", {})
        quality = prediction.get("quality", {})
        if not prediction:
            insufficient += 1
            continue
        if quality.get("filterPassed", True):
            passed += 1
        else:
            filtered += 1
        for issue in quality.get("filterIssues", []):
            label = str(issue).split("：", 1)[0]
            issues[label] = issues.get(label, 0) + 1
    return {
        "total": total,
        "passed": passed,
        "filtered": filtered,
        "insufficient": insufficient,
        "topIssues": sorted(
            [{"issue": key, "count": value} for key, value in issues.items()],
            key=lambda row: row["count"],
            reverse=True,
        )[:5],
    }


def append_source_note(source: str, note: str) -> str:
    parts = []
    for part in str(source).split("；"):
        if part and part not in parts:
            parts.append(part)
    if note not in parts:
        parts.append(note)
    return "；".join(parts)


def build_live_data(limit: int) -> dict[str, Any]:
    today = date.today()
    start = (today - timedelta(days=430)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    boards = retry("industry board list", ak.stock_board_industry_name_em)
    boards = boards.sort_values("涨跌幅", ascending=False).head(limit).reset_index(drop=True)
    fund_rank = retry("fund rank", lambda: ak.fund_open_fund_rank_em(symbol="全部"))

    industries: list[dict[str, Any]] = []
    for _, board in boards.iterrows():
        name = str(board["板块名称"])
        code = str(board["板块代码"])
        theme, fund_keywords = theme_for(name)

        hist = retry(
            f"industry history {name}",
            lambda code=code: ak.stock_board_industry_hist_em(
                symbol=code,
                start_date=start,
                end_date=end,
                period="日k",
                adjust="",
            ),
            attempts=2,
        )
        cons = retry(
            f"industry constituents {name}",
            lambda code=code: ak.stock_board_industry_cons_em(symbol=code),
            attempts=2,
        )

        cons = cons.sort_values("成交额", ascending=False).head(5)
        key_companies = [
            {"name": str(row["名称"]), "ticker": str(row["代码"])}
            for _, row in cons.head(3).iterrows()
        ]
        leader_name = str(board.get("领涨股票", ""))
        if leader_name and all(item["name"] != leader_name for item in key_companies):
            key_companies.append({"name": leader_name, "ticker": "领涨股"})

        day_return = clean_number(board.get("涨跌幅"))
        week_return = period_return(hist, 5)
        month_return = period_return(hist, 21)
        quarter_return = period_return(hist, 63)
        year_return = period_return(hist, 252)

        turnover = clean_number(board.get("换手率"))
        up_count = clean_number(board.get("上涨家数"))
        down_count = clean_number(board.get("下跌家数"))
        breadth = up_count / max(1, up_count + down_count) * 100
        heat = round(min(100, max(0, turnover * 14 + breadth * 0.45 + max(day_return, 0) * 4)), 1)
        trend_score = round(min(100, max(0, 50 + week_return * 3 + month_return * 1.2)), 1)
        capital_score = round(min(100, max(0, 45 + turnover * 9 + day_return * 3)), 1)
        valuation_percentile = percentile_rank(boards["总市值"], clean_number(board.get("总市值")))
        risk = drawdown_score(hist)
        funds = candidate_funds(fund_rank, fund_keywords)
        fund_themes = [f"{keyword}相关基金" for keyword in fund_keywords[:3]]

        industries.append(
            {
                "id": code.lower(),
                "name": name,
                "theme": theme,
                "defensive": theme in {"稳健现金流", "金融价值"},
                "heat": heat,
                "trendScore": trend_score,
                "capitalScore": capital_score,
                "valuationPercentile": valuation_percentile,
                "drawdownRisk": risk,
                "returns": {
                    "day": day_return,
                    "week": week_return,
                    "month": month_return,
                    "quarter": quarter_return,
                    "year": year_return,
                },
                "keyCompanies": key_companies,
                "fundThemes": fund_themes,
                "candidateFunds": funds,
                "view": make_view(name, theme, week_return, month_return, valuation_percentile, risk),
            }
        )

    return {
        "asOf": today.isoformat(),
        "source": SOURCE_NOTE,
        "isSample": False,
        "industries": industries,
        "fundSummary": summarize_funds(industries),
        "allocations": allocations(),
    }


def make_view(name: str, theme: str, week_return: float, month_return: float, valuation: float, risk: float) -> str:
    trend = "短期强势" if week_return > 2 and month_return > 0 else "仍需观察"
    price = "市值分位偏高" if valuation > 70 else "市值分位适中或偏低"
    risk_text = "回撤风险较高" if risk > 65 else "回撤风险相对可控"
    return f"{name}属于{theme}方向，当前{trend}，{price}，{risk_text}。基金配置建议以分批、限额和再平衡为主。"


def allocations() -> dict[str, list[dict[str, str]]]:
    return {
        "conservative": [
            {"role": "核心仓", "name": "沪深300 / 红利低波", "weight": "45%", "rationale": "以大盘宽基和分红资产承担权益核心，降低主题波动。"},
            {"role": "稳健仓", "name": "中短债 / 货币基金", "weight": "35%", "rationale": "保留流动性和回撤缓冲，给未来分批买入留空间。"},
            {"role": "增强仓", "name": "中证500 / 央企价值", "weight": "15%", "rationale": "补充中盘和价值风格，避免组合过度集中。"},
            {"role": "卫星仓", "name": "科技或医药主题", "weight": "5%", "rationale": "只小比例参与高波动赛道，防止情绪化追涨。"},
        ],
        "balanced": [
            {"role": "核心仓", "name": "沪深300 + 中证500", "weight": "45%", "rationale": "用宽基覆盖 A 股主要盈利资产，是组合的压舱部分。"},
            {"role": "防守仓", "name": "红利低波 / 央企红利", "weight": "20%", "rationale": "用现金流资产平滑主题基金的波动。"},
            {"role": "成长仓", "name": "半导体 / AI 算力 / 创新药", "weight": "25%", "rationale": "选择高景气赛道做卫星配置，分散到 2 到 3 个主题。"},
            {"role": "流动仓", "name": "债基 / 货币基金", "weight": "10%", "rationale": "用于再平衡、补仓和应对短期回撤。"},
        ],
        "aggressive": [
            {"role": "核心仓", "name": "沪深300 + 中证1000", "weight": "35%", "rationale": "保留基础宽基，但提高中小盘弹性。"},
            {"role": "成长仓", "name": "AI 算力 / 半导体", "weight": "30%", "rationale": "捕捉产业趋势，但必须接受较大净值波动。"},
            {"role": "轮动仓", "name": "创新药 / 有色 / 高端制造", "weight": "25%", "rationale": "基于行业评分滚动观察，避免所有仓位押在同一赛道。"},
            {"role": "现金仓", "name": "货币基金", "weight": "10%", "rationale": "保留应对深度回撤的再投入能力。"},
        ],
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update live A-share industry dashboard data.")
    parser.add_argument("--limit", type=int, default=20, help="number of top industries to fetch")
    args = parser.parse_args()

    try:
        payload = build_live_data(limit=args.limit)
    except Exception:
        if not OUTPUT.exists():
            raise
        with OUTPUT.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload = enrich_fund_predictions(payload)
        payload["source"] = append_source_note(payload["source"], CACHE_NOTE)
    write_json_atomic(OUTPUT, payload)
    print(f"wrote {OUTPUT} with {len(payload['industries'])} industries")
    print(f"source: {payload['source']}")


if __name__ == "__main__":
    main()
