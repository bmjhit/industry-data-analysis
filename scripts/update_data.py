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


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "industry-live.json"

SOURCE_NOTE = "AkShare 1.18.63；底层为东方财富公开行情与基金排行页面"

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
    quant_score = clamp(upside_probability * 0.55 + (100 - risk_score) * 0.35 + consistency * 0.10)

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
    }


def safe_fund_quant_metrics(code: str) -> dict[str, Any]:
    try:
        return fund_quant_metrics(code)
    except Exception:
        return {}


def fund_recommendation(metrics: dict[str, Any]) -> str:
    if not metrics:
        return "数据不足"
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
    payload["source"] = f"{payload.get('source', SOURCE_NOTE)}；个基量化预测已刷新"
    return payload


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
        payload["source"] = f"{payload['source']}；行业数据沿用上次成功缓存"
    write_json_atomic(OUTPUT, payload)
    print(f"wrote {OUTPUT} with {len(payload['industries'])} industries")
    print(f"source: {payload['source']}")


if __name__ == "__main__":
    main()
