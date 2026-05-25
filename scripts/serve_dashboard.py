#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 4174
DEFAULT_CACHE_FUNDS_PER_INDUSTRY = 30


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

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path not in {"/api/candidate-view", "/api/refresh"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = cached_candidate_view(payload) if self.path == "/api/candidate-view" else refresh_data(payload)
            self.write_json(HTTPStatus.OK, result)
        except Exception as exc:  # noqa: BLE001 - return readable UI error
            self.write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
    }


def main() -> None:
    port = clamp_int(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT, DEFAULT_PORT, 1024, 65535)
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Serving dashboard on http://127.0.0.1:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
