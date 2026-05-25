#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 4174
DEFAULT_CACHE_FUNDS_PER_INDUSTRY = 30
PURCHASES_PATH = ROOT / "data" / "purchases.json"
PURCHASE_EVALUATION_PATH = ROOT / "data" / "purchase-evaluation.json"


def clamp_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, number))


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/api/purchases":
            self.write_json(HTTPStatus.OK, purchase_payload())
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path not in {"/api/candidate-view", "/api/refresh", "/api/purchases", "/api/evaluate-purchases"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/candidate-view":
                result = cached_candidate_view(payload)
            elif self.path == "/api/refresh":
                result = refresh_data(payload)
            elif self.path == "/api/purchases":
                result = add_purchase(payload)
            else:
                result = evaluate_purchases(force=bool(payload.get("force")))
            self.write_json(HTTPStatus.OK, result)
        except Exception as exc:  # noqa: BLE001 - return readable UI error
            self.write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802 - http.server API
        if not self.path.startswith("/api/purchases/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            identifier = self.path.rsplit("/", 1)[-1]
            self.write_json(HTTPStatus.OK, delete_purchase(identifier))
        except Exception as exc:  # noqa: BLE001 - return readable UI error
            self.write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.replace(temporary, path)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def purchase_payload() -> dict[str, Any]:
    purchases = read_json(PURCHASES_PATH, {"purchases": []})
    evaluation = read_json(PURCHASE_EVALUATION_PATH, {})
    return {"ok": True, "purchases": purchases.get("purchases", []), "evaluation": evaluation}


def find_fund_snapshot(code: str) -> dict[str, Any]:
    live_path = ROOT / "data" / "industry-live.json"
    if not live_path.exists():
        return {}
    data = read_json(live_path, {})
    matches = []
    for industry in data.get("industries", []):
        for fund in industry.get("candidateFunds", []):
            if str(fund.get("code", "")).zfill(6) == code:
                matches.append((fund, industry))
    if not matches:
        return {}
    fund, industry = max(matches, key=lambda row: float(row[0].get("prediction", {}).get("quantScore", 0)))
    prediction = fund.get("prediction", {})
    return {
        "name": fund.get("name", code),
        "industry": industry.get("name"),
        "dataAsOf": data.get("asOf"),
        "recommendation": fund.get("recommendation"),
        "upsideProbability": prediction.get("upsideProbability"),
        "riskScore": prediction.get("riskScore"),
        "quantScore": prediction.get("quantScore"),
        "qualityScore": prediction.get("quality", {}).get("qualityScore"),
    }


def add_purchase(payload: dict[str, Any]) -> dict[str, Any]:
    code = str(payload.get("code", "")).strip().zfill(6)
    if not code.isdigit() or len(code) != 6:
        raise ValueError("基金代码应为 6 位数字")
    try:
        buy_date = date.fromisoformat(str(payload.get("buyDate", "")))
    except ValueError as exc:
        raise ValueError("买入日期格式无效") from exc
    if buy_date > date.today():
        raise ValueError("买入日期不能晚于今天")
    try:
        amount = round(float(payload.get("amount", 0)), 2)
    except (TypeError, ValueError) as exc:
        raise ValueError("买入金额无效") from exc
    if amount <= 0:
        raise ValueError("买入金额必须大于 0")

    snapshot = find_fund_snapshot(code)
    track_requested = bool(payload.get("trackStrategy", True))
    tracked = (
        track_requested
        and buy_date == date.today()
        and snapshot.get("dataAsOf") == buy_date.isoformat()
        and snapshot.get("recommendation") in {"优先观察", "可小额配置"}
    )
    record = {
        "id": uuid.uuid4().hex[:12],
        "code": code,
        "name": snapshot.get("name", str(payload.get("name", "")).strip() or code),
        "buyDate": buy_date.isoformat(),
        "amount": amount,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "strategySnapshot": {
            **snapshot,
            "tracked": tracked,
            "recordedDate": date.today().isoformat(),
            "note": "买入日记录的推荐信号" if tracked else "历史补录、非当日推荐或行情缓存非买入日，不纳入策略成功率",
        },
    }
    data = read_json(PURCHASES_PATH, {"purchases": []})
    data.setdefault("purchases", []).append(record)
    write_json_atomic(PURCHASES_PATH, data)
    return {"ok": True, "purchase": record, "evaluation": evaluate_purchases(force=True).get("evaluation", {})}


def delete_purchase(identifier: str) -> dict[str, Any]:
    data = read_json(PURCHASES_PATH, {"purchases": []})
    before = len(data.get("purchases", []))
    data["purchases"] = [row for row in data.get("purchases", []) if str(row.get("id")) != identifier]
    if len(data["purchases"]) == before:
        raise ValueError("未找到买入记录")
    write_json_atomic(PURCHASES_PATH, data)
    return {"ok": True, "evaluation": evaluate_purchases(force=True).get("evaluation", {})}


def evaluate_purchases(force: bool = False) -> dict[str, Any]:
    evaluation = read_json(PURCHASE_EVALUATION_PATH, {})
    if not force and evaluation.get("asOf") == date.today().isoformat():
        return {"ok": True, "evaluation": evaluation, "cached": True}
    process = subprocess.run(
        [sys.executable, "scripts/evaluate_purchases.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        "ok": True,
        "evaluation": read_json(PURCHASE_EVALUATION_PATH, {}),
        "cached": False,
        "output": process.stdout.strip(),
    }


def cached_candidate_view(payload: dict[str, Any]) -> dict[str, Any]:
    requested = clamp_int(payload.get("fundsPerIndustry"), DEFAULT_CACHE_FUNDS_PER_INDUSTRY, 1, 30)
    live_path = ROOT / "data" / "industry-live.json"
    if not live_path.exists():
        raise FileNotFoundError("暂无本地数据缓存，请先更新数据缓存")
    with live_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    industries = data.get("industries", [])
    cached_maximum = max((len(item.get("candidateFunds", [])) for item in industries), default=0)
    applied = min(requested, cached_maximum)
    for industry in industries:
        industry["candidateFunds"] = industry.get("candidateFunds", [])[:applied]

    coverage = data.setdefault("candidateCoverage", {})
    coverage.update(
        {
            "cacheLimit": cached_maximum,
            "requestedFundsPerIndustry": requested,
            "fundsPerIndustry": applied,
            "candidateCount": sum(len(item.get("candidateFunds", [])) for item in industries),
            "mode": "local-cache-view",
        }
    )
    return {
        "ok": True,
        "data": data,
        "cachedMaximum": cached_maximum,
        "appliedFundsPerIndustry": applied,
        "limitedByCache": requested > cached_maximum,
    }


def refresh_data(payload: dict[str, Any]) -> dict[str, Any]:
    funds_per_industry = clamp_int(
        payload.get("cacheFundsPerIndustry"),
        DEFAULT_CACHE_FUNDS_PER_INDUSTRY,
        1,
        30,
    )
    limit = clamp_int(payload.get("limit"), 12, 1, 50)
    fund_scan_limit = clamp_int(payload.get("fundScanLimit"), 120, 0, 1000)
    exposure_threshold = float(payload.get("exposureThreshold", 2.5))
    exposure_threshold = max(0.0, min(30.0, exposure_threshold))
    run_backtest = bool(payload.get("runBacktest", True))

    update_cmd = [
        sys.executable,
        "scripts/update_data.py",
        "--limit",
        str(limit),
        "--funds-per-industry",
        str(funds_per_industry),
        "--fund-scan-limit",
        str(fund_scan_limit),
        "--exposure-threshold",
        str(exposure_threshold),
    ]
    update = subprocess.run(update_cmd, cwd=ROOT, text=True, capture_output=True, check=True)

    backtest_output = ""
    if run_backtest:
        backtest = subprocess.run(
            [sys.executable, "scripts/backtest_short_term.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        backtest_output = backtest.stdout.strip()
    purchase_output = evaluate_purchases(force=True).get("output", "")

    coverage = {}
    live_path = ROOT / "data" / "industry-live.json"
    if live_path.exists():
        with live_path.open("r", encoding="utf-8") as handle:
            coverage = json.load(handle).get("candidateCoverage", {})

    return {
        "ok": True,
        "coverage": coverage,
        "updateOutput": update.stdout.strip(),
        "backtestOutput": backtest_output,
        "purchaseEvaluationOutput": purchase_output,
    }


def main() -> None:
    port = clamp_int(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT, DEFAULT_PORT, 1024, 65535)
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Serving dashboard on http://127.0.0.1:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
