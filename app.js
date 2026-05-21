const state = {
  period: "week",
  selectedIndustryId: null,
  riskProfile: "balanced",
  industries: [],
  allocations: {},
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
  const candidateFunds = selected.candidateFunds ?? [];
  document.querySelector("#candidateFundList").innerHTML = candidateFunds.length
    ? candidateFunds
        .map(
          (fund) => {
            const prediction = fund.prediction ?? {};
            const probability = prediction.upsideProbability ?? 0;
            const risk = prediction.riskScore ?? 0;
            const score = prediction.quantScore ?? 0;
            return `<li>
              <strong>${fund.name}</strong>
              <span class="muted">(${fund.code})</span>
              <div class="fund-metrics">
                <span>上涨概率 ${probability.toFixed(1)}%</span>
                <span>风险 ${risk.toFixed(1)}/100</span>
                <span>综合 ${score.toFixed(1)}</span>
                <span>${fund.recommendation ?? "观察"}</span>
              </div>
            </li>`;
          },
        )
        .join("")
    : "<li>暂无匹配基金候选</li>";
  document.querySelector("#detailNote").textContent = selected.view;
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

function render() {
  const industries = getVisibleIndustries();
  renderSummary(industries);
  renderIndustryList(industries);
  renderDetail(industries);
  renderAllocations();
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
  renderAllocations();
});

loadData();
