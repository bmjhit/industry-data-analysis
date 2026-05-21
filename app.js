const state = {
  period: "week",
  selectedIndustryId: null,
  riskProfile: "balanced",
  industries: [],
  allocations: {},
  fundSummary: {},
};

const periodLabels = {
  day: "日",
  week: "周",
  month: "月",
  quarter: "季",
  year: "年",
};

const riskLabels = {
  conservative: "稳健型",
  balanced: "均衡型",
  aggressive: "进取型",
};

const riskRules = {
  conservative: {
    maxRisk: 45,
    minQuality: 85,
    minQuant: 62,
    allowFiltered: false,
    cycle: "6-18 个月",
  },
  balanced: {
    maxRisk: 60,
    minQuality: 75,
    minQuant: 58,
    allowFiltered: false,
    cycle: "3-12 个月",
  },
  aggressive: {
    maxRisk: 75,
    minQuality: 65,
    minQuant: 55,
    allowFiltered: false,
    cycle: "1-6 个月",
  },
};

const formatPercent = (value) => `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;

const signalClass = (value) => {
  if (value > 0.4) return "up";
  if (value < -0.4) return "down";
  return "neutral";
};

const scoreIndustry = (industry, period) => {
  const periodReturn = industry.returns[period] ?? 0;
  const valuationScore = 100 - industry.valuationPercentile;
  return (
    periodReturn * 5 +
    industry.trendScore * 0.32 +
    industry.capitalScore * 0.24 +
    valuationScore * 0.18 -
    industry.drawdownRisk * 0.16
  );
};

const getSignal = (score) => {
  if (score >= 42) return "偏强";
  if (score >= 26) return "中性偏强";
  if (score >= 12) return "中性";
  return "偏弱";
};

async function loadData() {
  const data = await fetchDashboardData();
  state.industries = data.industries;
  state.allocations = data.allocations;
  state.fundSummary = data.fundSummary ?? {};
  state.selectedIndustryId = data.industries[0].id;
  const sourceLabel = data.isSample ? "示例" : "真实";
  document.querySelector("#dataDate").textContent = `数据日期：${data.asOf}（${sourceLabel}）`;
  document.querySelector("#dataSource").textContent = data.source ?? "本地示例数据";
  render();
}

async function fetchDashboardData() {
  try {
    const response = await fetch("./data/industry-live.json", { cache: "no-store" });
    if (response.ok) {
      return response.json();
    }
  } catch (error) {
    console.warn("Live data unavailable, falling back to sample data.", error);
  }
  const fallback = await fetch("./data/industry-sample.json", { cache: "no-store" });
  return fallback.json();
}

function getVisibleIndustries() {
  const query = document.querySelector("#searchInput").value.trim().toLowerCase();
  return state.industries
    .filter((industry) => {
      const haystack = [
        industry.name,
        industry.theme,
        ...industry.keyCompanies.map((company) => company.name),
      ]
        .join(" ")
        .toLowerCase();
      return !query || haystack.includes(query);
    })
    .map((industry) => ({
      ...industry,
      score: scoreIndustry(industry, state.period),
    }))
    .sort((a, b) => b.score - a.score);
}

function renderSummary(industries) {
  if (!industries.length) {
    document.querySelector("#strongSector").textContent = "--";
    document.querySelector("#defenseSector").textContent = "--";
    document.querySelector("#avgHeat").textContent = "--";
    document.querySelector("#portfolioTilt").textContent = "等待筛选";
    return;
  }
  const strongest = industries[0];
  const defense = industries.find((industry) => industry.defensive) ?? industries.at(-1);
  const avgHeat = industries.reduce((sum, item) => sum + item.heat, 0) / industries.length;
  const positiveCount = industries.filter((item) => item.returns[state.period] > 0).length;

  document.querySelector("#strongSector").textContent = strongest?.name ?? "--";
  document.querySelector("#defenseSector").textContent = defense?.name ?? "--";
  document.querySelector("#avgHeat").textContent = `${avgHeat.toFixed(0)}/100`;
  document.querySelector("#portfolioTilt").textContent =
    positiveCount >= industries.length * 0.62 ? "提高权益仓位" : "均衡防守";
}

function renderIndustryList(industries) {
  const container = document.querySelector("#industryList");
  document.querySelector("#industryCount").textContent = `${industries.length} 个行业`;
  if (!industries.length) {
    container.innerHTML = '<div class="empty-state">没有匹配的行业或企业</div>';
    return;
  }
  container.innerHTML = industries
    .map((industry) => {
      const periodReturn = industry.returns[state.period];
      const active = industry.id === state.selectedIndustryId ? "active" : "";
      return `
        <button class="industry-item ${active}" data-id="${industry.id}">
          <div class="industry-top">
            <span>${industry.name}</span>
            <span class="${signalClass(periodReturn)}">${formatPercent(periodReturn)}</span>
          </div>
          <div class="industry-meta">
            <span>${industry.theme}</span>
            <span>评分 ${industry.score.toFixed(0)} · ${getSignal(industry.score)}</span>
          </div>
        </button>
      `;
    })
    .join("");
}

function renderDetail(industries) {
  const selected =
    industries.find((industry) => industry.id === state.selectedIndustryId) ?? industries[0];
  if (!selected) {
    document.querySelector("#detailTitle").textContent = "--";
    document.querySelector("#signalBadge").textContent = "--";
    document.querySelector("#detailReturn").textContent = "--";
    document.querySelector("#detailValuation").textContent = "--";
    document.querySelector("#detailCapital").textContent = "--";
    document.querySelector("#detailRisk").textContent = "--";
    document.querySelector("#trendValue").textContent = "--";
    document.querySelector("#trendBar").style.width = "0";
    document.querySelector("#companyList").innerHTML = "";
    document.querySelector("#fundThemeList").innerHTML = "";
    document.querySelector("#detailNote").textContent = "请调整搜索条件后继续查看。";
    return;
  }
  state.selectedIndustryId = selected.id;
  const periodReturn = selected.returns[state.period];
  const signal = getSignal(selected.score);
  const trendWidth = Math.max(4, Math.min(100, selected.trendScore));

  document.querySelector("#detailTitle").textContent = selected.name;
  document.querySelector("#signalBadge").textContent = signal;
  document.querySelector("#detailReturn").textContent = formatPercent(periodReturn);
  document.querySelector("#detailReturn").className = signalClass(periodReturn);
  document.querySelector("#detailValuation").textContent = `${selected.valuationPercentile}%`;
  document.querySelector("#detailCapital").textContent = `${selected.capitalScore}/100`;
  document.querySelector("#detailRisk").textContent = `${selected.drawdownRisk}/100`;
  document.querySelector("#trendValue").textContent = `${selected.trendScore}/100`;
  document.querySelector("#trendBar").style.width = `${trendWidth}%`;
  document.querySelector("#companyList").innerHTML = selected.keyCompanies
    .map((company) => `<li>${company.name} <span class="muted">(${company.ticker})</span></li>`)
    .join("");
  document.querySelector("#fundThemeList").innerHTML = selected.fundThemes
    .map((theme) => `<li>${theme}</li>`)
    .join("");
  const candidateFunds = rankFundsForRisk(selected.candidateFunds ?? [], state.riskProfile);
  document.querySelector("#candidateFundList").innerHTML = candidateFunds.length
    ? candidateFunds
        .map(
          (fund) => {
            const prediction = fund.prediction ?? {};
            const probability = prediction.upsideProbability ?? 0;
            const risk = prediction.riskScore ?? 0;
            const score = prediction.quantScore ?? 0;
            const quality = prediction.quality ?? {};
            const qualityScore = quality.qualityScore ?? 0;
            const issues = quality.filterIssues ?? [];
            const missing = quality.missingFields ?? [];
            return `<li>
              <strong>${fund.name}</strong>
              <span class="muted">(${fund.code})</span>
              <div class="fund-metrics">
                <span>上涨概率 ${probability.toFixed(1)}%</span>
                <span>风险 ${risk.toFixed(1)}/100</span>
                <span>综合 ${score.toFixed(1)}</span>
                <span>质量 ${qualityScore.toFixed(1)}</span>
                <span>${fund.recommendation ?? "观察"}</span>
              </div>
              <div class="fund-quality">
                <span>规模 ${formatNullable(quality.scaleYi, "亿")}</span>
                <span>成立 ${quality.inceptionDate ?? "--"}</span>
                <span>经理 ${formatManagers(quality.managerNames)}</span>
                <span>前十大 ${formatNullable(quality.top10HoldingPct, "%")}</span>
                <span>跟踪误差 ${formatNullable(quality.trackingError, "%")}</span>
                <span>基准 ${quality.trackingBenchmark ?? "--"}</span>
              </div>
              ${issues.length ? `<p class="fund-warning">过滤原因：${issues.join("；")}</p>` : ""}
              ${missing.length ? `<p class="fund-missing">缺失字段：${missing.join("、")}</p>` : ""}
              <p class="fund-missing">建议周期：${holdingPeriodForFund(fund, state.riskProfile, selected)}</p>
            </li>`;
          },
        )
        .join("")
    : "<li>暂无匹配基金候选</li>";
  document.querySelector("#detailNote").textContent = selected.view;
}

function rankFundsForRisk(funds, riskProfile) {
  const rules = riskRules[riskProfile] ?? riskRules.balanced;
  return funds
    .filter((fund) => passesRiskProfile(fund, rules))
    .map((fund) => ({ ...fund, riskAdjustedScore: fundScoreForRisk(fund, riskProfile) }))
    .sort((a, b) => b.riskAdjustedScore - a.riskAdjustedScore);
}

function passesRiskProfile(fund, rules) {
  const prediction = fund.prediction ?? {};
  const quality = prediction.quality ?? {};
  if (!rules.allowFiltered && quality.filterPassed === false) return false;
  if ((prediction.riskScore ?? 100) > rules.maxRisk) return false;
  if ((quality.qualityScore ?? 0) < rules.minQuality) return false;
  if ((prediction.quantScore ?? 0) < rules.minQuant) return false;
  return true;
}

function fundScoreForRisk(fund, riskProfile) {
  const prediction = fund.prediction ?? {};
  const quality = prediction.quality ?? {};
  const probability = prediction.upsideProbability ?? 0;
  const risk = prediction.riskScore ?? 100;
  const quant = prediction.quantScore ?? 0;
  const qualityScore = quality.qualityScore ?? 0;
  if (riskProfile === "conservative") {
    return quant * 0.36 + qualityScore * 0.34 + (100 - risk) * 0.22 + probability * 0.08;
  }
  if (riskProfile === "aggressive") {
    return quant * 0.44 + probability * 0.30 + qualityScore * 0.16 + (100 - risk) * 0.10;
  }
  return quant * 0.42 + qualityScore * 0.25 + probability * 0.20 + (100 - risk) * 0.13;
}

function holdingPeriodForFund(fund, riskProfile, industry) {
  const prediction = fund.prediction ?? {};
  const risk = prediction.riskScore ?? 100;
  const momentum60 = prediction.momentum60 ?? 0;
  const drawdown = prediction.maxDrawdown ?? 0;
  if (riskProfile === "conservative") return risk <= 35 ? "9-18 个月" : "6-12 个月";
  if (riskProfile === "aggressive") return momentum60 >= 20 && industry.score >= 70 ? "1-3 个月" : "3-6 个月";
  if (drawdown <= 12 && risk <= 45) return "6-12 个月";
  return "3-9 个月";
}

function formatNullable(value, suffix) {
  return Number.isFinite(value) ? `${value.toFixed(2)}${suffix}` : "--";
}

function formatManagers(names = []) {
  return names.length ? names.join("、") : "--";
}

function renderAllocations() {
  const allocation = state.allocations[state.riskProfile] ?? [];
  document.querySelector("#riskLabel").textContent = riskLabels[state.riskProfile];
  document.querySelector("#allocationGrid").innerHTML = allocation
    .map(
      (item) => `
        <article class="allocation-card">
          <span>${item.role}</span>
          <strong>${item.weight}</strong>
          <h3>${item.name}</h3>
          <p>${item.rationale}</p>
        </article>
      `,
    )
    .join("");
}

function renderFundSummary() {
  const summary = summarizeFundsForRisk(state.riskProfile);
  const total = summary.total ?? 0;
  const passed = summary.passed ?? 0;
  const filtered = summary.filtered ?? 0;
  const insufficient = summary.insufficient ?? 0;
  document.querySelector("#fundSummary").innerHTML = `
    <article><span>候选基金</span><strong>${total}</strong></article>
    <article><span>通过过滤</span><strong>${passed}</strong></article>
    <article><span>过滤淘汰</span><strong>${filtered}</strong></article>
    <article><span>数据不足</span><strong>${insufficient}</strong></article>
  `;
  document.querySelector("#fundIssueList").innerHTML = (summary.topIssues ?? []).length
    ? summary.topIssues
        .map((item) => `<li>${item.issue} <span class="muted">${item.count} 只</span></li>`)
        .join("")
    : "<li>暂无主要过滤问题</li>";
}

function summarizeFundsForRisk(riskProfile) {
  const allFunds = state.industries.flatMap((industry) => industry.candidateFunds ?? []);
  const rules = riskRules[riskProfile] ?? riskRules.balanced;
  const passedFunds = allFunds.filter((fund) => passesRiskProfile(fund, rules));
  const issues = {};
  let filtered = 0;
  let insufficient = 0;

  for (const fund of allFunds) {
    const prediction = fund.prediction ?? {};
    const quality = prediction.quality ?? {};
    if (!Object.keys(prediction).length) {
      insufficient += 1;
      continue;
    }
    if (!passesRiskProfile(fund, rules)) {
      filtered += 1;
      const labels = [];
      if (quality.filterPassed === false) labels.push(...(quality.filterIssues ?? []).map((issue) => issue.split("：")[0]));
      if ((prediction.riskScore ?? 100) > rules.maxRisk) labels.push("风险分超限");
      if ((quality.qualityScore ?? 0) < rules.minQuality) labels.push("质量分不足");
      if ((prediction.quantScore ?? 0) < rules.minQuant) labels.push("综合分不足");
      for (const label of labels.length ? labels : ["不符合风险偏好"]) {
        issues[label] = (issues[label] ?? 0) + 1;
      }
    }
  }

  return {
    total: allFunds.length,
    passed: passedFunds.length,
    filtered,
    insufficient,
    topIssues: Object.entries(issues)
      .map(([issue, count]) => ({ issue, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 5),
  };
}

function renderDailyRecommendations(industries) {
  const candidates = industries.flatMap((industry) =>
    rankFundsForRisk(industry.candidateFunds ?? [], state.riskProfile).map((fund) => ({
      ...fund,
      industryName: industry.name,
      industryScore: industry.score,
      holdingPeriod: holdingPeriodForFund(fund, state.riskProfile, industry),
      finalScore: fundScoreForRisk(fund, state.riskProfile) + industry.score * 0.18,
    })),
  );
  const picks = candidates.sort((a, b) => b.finalScore - a.finalScore).slice(0, 3);
  document.querySelector("#dailyFundPicks").innerHTML = picks.length
    ? picks
        .map((fund) => {
          const prediction = fund.prediction ?? {};
          const quality = prediction.quality ?? {};
          return `
            <article class="pick-card">
              <span>${fund.industryName}</span>
              <strong>${fund.name}</strong>
              <div class="fund-metrics">
                <span>上涨概率 ${(prediction.upsideProbability ?? 0).toFixed(1)}%</span>
                <span>风险 ${(prediction.riskScore ?? 0).toFixed(1)}</span>
                <span>质量 ${(quality.qualityScore ?? 0).toFixed(1)}</span>
              </div>
              <p>建议周期：${fund.holdingPeriod}</p>
            </article>
          `;
        })
        .join("")
    : '<div class="empty-state">当前风险偏好下暂无满足条件的推荐基金</div>';
}

function render() {
  const industries = getVisibleIndustries();
  renderSummary(industries);
  renderIndustryList(industries);
  renderDetail(industries);
  renderAllocations();
  renderFundSummary();
  renderDailyRecommendations(industries);
}

document.querySelector("#periodTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-period]");
  if (!button) return;
  state.period = button.dataset.period;
  document
    .querySelectorAll("#periodTabs button")
    .forEach((tab) => tab.classList.toggle("active", tab.dataset.period === state.period));
  render();
});

document.querySelector("#industryList").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-id]");
  if (!button) return;
  state.selectedIndustryId = button.dataset.id;
  render();
});

document.querySelector("#searchInput").addEventListener("input", render);

document.querySelector("#riskSelect").addEventListener("change", (event) => {
  state.riskProfile = event.target.value;
  render();
});

loadData();
