const state = {
  chart: null,
  history: [],
  latestStatus: null,
  latestStrategies: null,
  latestExchange: null,
  backtestResult: null,
  backtestError: "",
  backtestLoading: false,
  backtestForm: null,
}

const DISPLAY_TIME_ZONE = "America/Bogota"

const formatMoney = (value) =>
  new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(
    Number(value || 0),
  )
const formatNumber = (value, digits = 2) => Number(value || 0).toFixed(digits)

function parseApiError(message) {
  try {
    const payload = JSON.parse(message)
    return payload.detail || message
  } catch {
    return message
  }
}

function totalTrades(reportItem) {
  return (reportItem.symbol_reports || []).reduce((sum, symbolReport) => sum + (symbolReport.trades || []).length, 0)
}

function averagePnlPerTrade(reportItem) {
  const trades = totalTrades(reportItem)
  if (!trades) {
    return 0
  }
  return Number(reportItem.net_pnl || 0) / trades
}

function findBestAndWorstSymbols(reportItem) {
  const reports = reportItem.symbol_reports || []
  if (!reports.length) {
    return { best: null, worst: null }
  }
  const sorted = [...reports].sort((a, b) => Number(a.net_pnl || 0) - Number(b.net_pnl || 0))
  return {
    worst: sorted[0],
    best: sorted[sorted.length - 1],
  }
}

function verdictText(reportItem) {
  const pnl = Number(reportItem.net_pnl || 0)
  const drawdown = Number(reportItem.max_drawdown || 0)
  const winRate = Number(reportItem.win_rate || 0)

  if (pnl > 0 && drawdown <= 10 && winRate >= 45) {
    return "Healthy: profitable with controlled drawdown."
  }
  if (pnl > 0 && drawdown > 10) {
    return "Profitable but volatile: review risk settings before live use."
  }
  if (pnl <= 0 && winRate >= 45) {
    return "Many wins but poor payoff ratio: tune exits and risk/reward."
  }
  return "Weak run: strategy needs adjustment for this market window."
}

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options })
  if (!response.ok) {
    throw new Error(await response.text())
  }
  return response.json()
}

function metricCard(label, value, tone = "") {
  return `<div class="metric"><div class="label">${label}</div><div class="value ${tone}">${value}</div></div>`
}

function renderMetrics(metrics) {
  const root = document.getElementById("metrics")
  root.innerHTML = [
    metricCard("Equity", formatMoney(metrics.equity)),
    metricCard("Realized PnL", formatMoney(metrics.realized_pnl), metrics.realized_pnl >= 0 ? "positive" : "negative"),
    metricCard(
      "Unrealized PnL",
      formatMoney(metrics.unrealized_pnl),
      metrics.unrealized_pnl >= 0 ? "positive" : "negative",
    ),
    metricCard("Margin Used", formatMoney(metrics.margin_in_use)),
    metricCard(
      "Liquidation Risk",
      `${formatNumber(metrics.liquidation_risk)}%`,
      metrics.liquidation_risk >= 80 ? "negative" : "",
    ),
    metricCard("Open Positions", String(metrics.open_positions)),
  ].join("")
}

function renderBotState(payload) {
  const root = document.getElementById("botState")
  const state = payload.state || {}
  const profile = payload.profile || {}
  const lastCycle = state.last_run_at
    ? `<div class="pill">Last engine cycle: ${formatUtcHuman(state.last_run_at)}</div>`
    : ""
  root.innerHTML = `
    <div class="pill">Profile: ${profile.name || "n/a"}</div>
    <div class="pill">Started: ${formatUtcHuman(state.started_at)}</div>
    ${lastCycle}
    <div class="pill">Symbols: ${(state.active_symbols || []).join(", ") || "n/a"}</div>
    <div class="pill">Last error: ${state.last_error || "none"}</div>
  `
}

function renderExchangeOverview(data) {
  const root = document.getElementById("exchangeOverview")
  if (!data) {
    root.innerHTML = '<p class="muted">No exchange data loaded.</p>'
    return
  }
  const prices = Object.entries(data.latest_prices || {})
    .map(
      ([symbol, price]) => `<div class="pill">${symbol}: ${price == null ? "unavailable" : formatMoney(price)}</div>`,
    )
    .join("")

  root.innerHTML = `
    <div class="pill">Mode: ${data.mode || "n/a"}</div>
    <div class="pill">Environment: ${data.testnet ? "Testnet" : "Live / Mainnet"}</div>
    <div class="pill">Quote asset: ${data.quote_asset || "n/a"}</div>
    <div class="pill">Interval: ${data.interval || "n/a"}</div>
    <div class="pill">Candle style: ${data.candle_style || "n/a"}</div>
    <div class="pill">Leverage: ${data.leverage || "n/a"}x / Max ${data.max_leverage || "n/a"}x</div>
    <div class="pill">Endpoint: ${data.base_url || "n/a"}</div>
    <div class="stack inline-pills">${prices || '<p class="muted">No prices available.</p>'}</div>
  `
}

function formatUtcHuman(value) {
  if (!value) {
    return "never"
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: DISPLAY_TIME_ZONE,
    timeZoneName: "short",
  }).format(date)
}

function formatRuntime(startedAt) {
  if (!startedAt) {
    return "Stopped"
  }
  const date = new Date(startedAt)
  if (Number.isNaN(date.getTime())) {
    return "Stopped"
  }
  const totalSeconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000))
  const hours = String(Math.floor(totalSeconds / 3600)).padStart(2, "0")
  const minutes = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0")
  const seconds = String(totalSeconds % 60).padStart(2, "0")
  return `Running ${hours}:${minutes}:${seconds}`
}

function renderHeroControls() {
  const botState = state.latestStatus?.state || {}
  const running = Boolean(botState.running)
  const paused = Boolean(botState.paused)
  const startButton = document.getElementById("startButton")
  const stopButton = document.getElementById("stopButton")
  const pauseButton = document.getElementById("pauseButton")
  const runOnceButton = document.getElementById("runOnceButton")
  const runtimeTimer = document.getElementById("runtimeTimer")

  if (startButton) {
    startButton.disabled = running
  }
  if (stopButton) {
    stopButton.disabled = !running
  }
  if (pauseButton) {
    pauseButton.disabled = !running
    pauseButton.dataset.action = paused ? "resume" : "pause"
    pauseButton.textContent = paused ? "Resume" : "Pause"
  }
  if (runOnceButton) {
    runOnceButton.disabled = running
  }
  if (runtimeTimer) {
    runtimeTimer.textContent = running ? formatRuntime(botState.started_at) : "Stopped"
  }
}

function renderHistorySummary() {
  const root = document.getElementById("historySummary")
  if (!root) {
    return
  }
  if (!state.history.length) {
    root.innerHTML = '<p class="muted">No stored snapshot history found.</p>'
    return
  }
  const first = state.history[0]
  const last = state.history[state.history.length - 1]
  root.innerHTML = `
    <div class="pill">Loaded snapshots: ${state.history.length}</div>
    <div class="pill">From: ${first.rawLabel ? formatUtcHuman(first.rawLabel) : first.label}</div>
    <div class="pill">To: ${last.rawLabel ? formatUtcHuman(last.rawLabel) : last.label}</div>
    <div class="pill">Timezone: UTC-5</div>
  `
}

function renderPositions(positions) {
  const root = document.getElementById("positions")
  if (!positions.length) {
    root.innerHTML = '<p class="muted">No open positions.</p>'
    return
  }
  root.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Mark</th><th>PnL</th><th>Action</th>
          </tr>
        </thead>
        <tbody>
          ${positions
            .map(
              (position) => `
            <tr>
              <td>${position.symbol}</td>
              <td>${position.side}</td>
              <td>${formatNumber(position.quantity, 4)}</td>
              <td>${formatMoney(position.entry_price)}</td>
              <td>${formatMoney(position.current_price)}</td>
              <td class="${position.unrealized_pnl >= 0 ? "positive" : "negative"}">${formatMoney(position.unrealized_pnl)}</td>
              <td><button class="danger" data-close-symbol="${position.symbol}">Close</button></td>
            </tr>
          `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `
}

function renderTrades(trades) {
  const root = document.getElementById("trades")
  if (!trades.length) {
    root.innerHTML = '<p class="muted">No trades recorded yet.</p>'
    return
  }
  root.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th><th>Symbol</th><th>Side</th><th>Status</th><th>PnL</th>
          </tr>
        </thead>
        <tbody>
          ${trades
            .map(
              (trade) => `
            <tr>
              <td>${formatUtcHuman(trade.closed_at || trade.opened_at)}</td>
              <td>${trade.symbol}</td>
              <td>${trade.side}</td>
              <td>${trade.status}</td>
              <td class="${Number(trade.realized_pnl || 0) >= 0 ? "positive" : "negative"}">${formatMoney(trade.realized_pnl)}</td>
            </tr>
          `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `
}

function renderStrategies(data) {
  const root = document.getElementById("strategies")
  const profile = data.active || {}
  const running = Boolean(state.latestStatus?.state?.running)
  const savedProfiles = data.saved || []
  const options = savedProfiles
    .map((name) => `<option value="${name}" ${name === profile.name ? "selected" : ""}>${name}</option>`)
    .join("")
  root.innerHTML = `
    <div class="pill">Active profile: ${profile.name || "n/a"}</div>
    <div class="pill">Threshold: ${profile.threshold ?? "n/a"}</div>
    <div class="pill">Description: ${profile.description || "n/a"}</div>
    <div class="pill">Saved: ${(data.saved || []).join(", ") || "none"}</div>
    <div class="form-grid strategy-grid">
      <label>
        Change strategy profile
        <select id="strategyProfileSelect" ${running ? "disabled" : ""}>
          ${options || '<option value="">No saved profiles</option>'}
        </select>
      </label>
      <div class="actions-row">
        <button data-action="load-strategy" ${running ? "disabled" : ""}>Load Strategy</button>
      </div>
    </div>
    <div class="pill muted">${running ? "Stop the bot to change strategy profile." : "Bot stopped: strategy can be changed."}</div>
    <div class="pill"><button data-action="seed-strategy">Save Default Profile</button></div>
  `
}

function renderBacktestControls() {
  const root = document.getElementById("backtestControls")
  const savedProfiles = state.latestStrategies?.saved || []
  const activeProfile = state.latestStrategies?.active?.name || "default"
  const symbols = state.latestStatus?.config?.symbols || []
  const interval = state.latestStatus?.config?.interval || "5m"
  const candlesLimit = Number(state.latestStatus?.config?.candles_limit || 200)
  const leverage = Number(state.latestStatus?.config?.leverage || 3)

  if (!state.backtestForm) {
    state.backtestForm = {
      profile: activeProfile,
      compareText: "",
      symbolsText: symbols.join(","),
      interval,
      candlesLimit,
      leverage,
    }
  }

  const profileOptions = [activeProfile, ...savedProfiles]
    .filter((name, index, arr) => name && arr.indexOf(name) === index)
    .map((name) => `<option value="${name}">${name}</option>`)
    .join("")

  root.innerHTML = `
    <div class="form-grid">
      <label>
        Profile
        <select id="backtestProfile" ${state.backtestLoading ? "disabled" : ""}>${profileOptions || '<option value="default">default</option>'}</select>
      </label>
      <label>
        Compare (comma-separated built-ins)
        <input id="backtestCompare" type="text" value="${state.backtestForm.compareText}" placeholder="ema_cross,macd,rsi" ${state.backtestLoading ? "disabled" : ""} />
      </label>
      <label>
        Symbols (comma-separated)
        <input id="backtestSymbols" type="text" value="${state.backtestForm.symbolsText}" placeholder="BTCUSDT,ETHUSDT" ${state.backtestLoading ? "disabled" : ""} />
      </label>
      <label>
        Interval
        <input id="backtestInterval" type="text" value="${state.backtestForm.interval}" placeholder="5m,15m,1h,4h" ${state.backtestLoading ? "disabled" : ""} />
      </label>
      <label>
        Candles limit
        <input id="backtestCandlesLimit" type="number" min="30" max="1500" value="${state.backtestForm.candlesLimit}" ${state.backtestLoading ? "disabled" : ""} />
      </label>
      <label>
        Leverage
        <input id="backtestLeverage" type="number" min="1" max="125" value="${state.backtestForm.leverage}" ${state.backtestLoading ? "disabled" : ""} />
      </label>
      <div class="actions-row">
        <button data-action="run-backtest" class="secondary" ${state.backtestLoading ? "disabled" : ""}>
          ${state.backtestLoading ? '<span class="spinner"></span> Running...' : "Run Backtest"}
        </button>
      </div>
    </div>
  `

  const profileSelect = document.getElementById("backtestProfile")
  if (profileSelect) {
    profileSelect.value = state.backtestForm.profile
  }
}

function captureBacktestFormFromDom() {
  state.backtestForm = {
    profile: document.getElementById("backtestProfile")?.value || state.backtestForm?.profile || "default",
    compareText: document.getElementById("backtestCompare")?.value || "",
    symbolsText: document.getElementById("backtestSymbols")?.value || "",
    interval: document.getElementById("backtestInterval")?.value?.trim() || "5m",
    candlesLimit: Number(document.getElementById("backtestCandlesLimit")?.value || 200),
    leverage: Number(document.getElementById("backtestLeverage")?.value || 3),
  }
}

function renderBacktestResult() {
  const root = document.getElementById("backtestResult")
  if (state.backtestLoading) {
    root.innerHTML = '<div class="pill"><span class="spinner"></span> Backtest is running, please wait...</div>'
    return
  }
  if (state.backtestError) {
    root.innerHTML = `<div class="pill negative">${state.backtestError}</div>`
    return
  }
  const data = state.backtestResult
  if (!data) {
    root.innerHTML = '<p class="muted">No backtest run yet.</p>'
    return
  }

  const reports = data.report?.reports || []
  const context = data.context || {}
  const compared = (context.compare || []).filter(Boolean)
  const runMode = compared.length ? "Strategy comparison" : "Single profile test"
  const cards = reports
    .map(
      (item) => `
      <div class="metric compact">
        <div class="label">${item.profile_name}</div>
        <div class="value ${item.net_pnl >= 0 ? "positive" : "negative"}">${formatMoney(item.net_pnl)}</div>
        <div class="muted">Win rate: ${formatNumber(item.win_rate, 1)}% · Max drawdown: ${formatNumber(item.max_drawdown, 1)}%</div>
        <div class="muted">Trades: ${totalTrades(item)} · Avg per trade: ${formatMoney(averagePnlPerTrade(item))}</div>
        <div class="muted"><strong>Interpretation:</strong> ${verdictText(item)}</div>
      </div>
    `,
    )
    .join("")

  const details = reports
    .map((item) => {
      const summary = findBestAndWorstSymbols(item)
      const best = summary.best ? `${summary.best.symbol} (${formatMoney(summary.best.net_pnl)})` : "n/a"
      const worst = summary.worst ? `${summary.worst.symbol} (${formatMoney(summary.worst.net_pnl)})` : "n/a"
      const symbolRows = (item.symbol_reports || [])
        .map(
          (symbolReport) => `
          <tr>
            <td>${symbolReport.symbol}</td>
            <td>${(symbolReport.trades || []).length}</td>
            <td class="${Number(symbolReport.net_pnl || 0) >= 0 ? "positive" : "negative"}">${formatMoney(symbolReport.net_pnl)}</td>
            <td>${formatNumber(symbolReport.win_rate, 1)}%</td>
            <td>${formatNumber(symbolReport.max_drawdown, 1)}%</td>
          </tr>
        `,
        )
        .join("")

      return `
        <div class="panel-subsection">
          <h3>${item.profile_name} breakdown</h3>
          <div class="pill">Best symbol: ${best}</div>
          <div class="pill">Worst symbol: ${worst}</div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Symbol</th><th>Trades</th><th>Net PnL</th><th>Win rate</th><th>Max drawdown</th>
                </tr>
              </thead>
              <tbody>
                ${symbolRows || '<tr><td colspan="5" class="muted">No symbol data</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>
      `
    })
    .join("")

  root.innerHTML = `
    <div class="pill"><strong>${runMode}</strong></div>
    <div class="pill">Window: ${context.interval || "n/a"} × ${context.candles_limit || "n/a"} candles</div>
    <div class="pill">Leverage: ${context.leverage || "n/a"}x</div>
    <div class="pill">Symbols: ${(context.symbols || []).join(", ") || "n/a"}</div>
    <div class="pill">Report: ${data.path || "n/a"}</div>
    <div class="metrics stack-metrics">${cards}</div>
    <div class="muted">How to read this: Net PnL is total simulated profit/loss. Win rate is percent of profitable trades. Max drawdown is the worst peak-to-trough equity decline.</div>
    ${details}
  `
}

function renderSignals(payload) {
  const root = document.getElementById("signals")
  const actions = payload.state?.latest_actions || {}
  const scores = payload.state?.latest_scores || {}
  const reasons = payload.state?.latest_reasons || {}
  const items = Object.keys(actions).map(
    (symbol) => `
    <div class="pill">
      <strong>${symbol}</strong> · ${actions[symbol]} · ${formatNumber(scores[symbol] || 0, 3)}
      <span class="muted">${(reasons[symbol] || []).join(" | ")}</span>
    </div>
  `,
  )
  root.innerHTML = items.length ? items.join("") : '<p class="muted">No signals yet.</p>'
}

function updateChart(metrics) {
  const ctx = document.getElementById("performanceChart")
  const labels = state.history.map((point) => point.label)
  const equitySeries = state.history.map((point) => point.equity)
  const pnlSeries = state.history.map((point) => point.pnl)

  if (!state.chart) {
    state.chart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          { label: "Equity", data: equitySeries, borderColor: "#6ee7b7", tension: 0.35, fill: false },
          { label: "PnL", data: pnlSeries, borderColor: "#60a5fa", tension: 0.35, fill: false },
        ],
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { color: "#e5eefc" } } },
        scales: {
          x: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148, 163, 184, 0.12)" } },
          y: { ticks: { color: "#94a3b8" }, grid: { color: "rgba(148, 163, 184, 0.12)" } },
        },
      },
    })
    return
  }
  state.chart.data.labels = labels
  state.chart.data.datasets[0].data = equitySeries
  state.chart.data.datasets[1].data = pnlSeries
  state.chart.update()
}

function updateHistoryFromSnapshots(snapshots, currentMetrics) {
  const rows = (snapshots || []).map((snapshot, index) => {
    const metrics = snapshot.metrics || {}
    const label = snapshot.created_at || String(index + 1)
    return {
      label: formatUtcHuman(label),
      rawLabel: label,
      equity: Number(metrics.equity || 0),
      pnl: Number(metrics.realized_pnl || 0) + Number(metrics.unrealized_pnl || 0),
    }
  })

  if (!rows.length && currentMetrics) {
    rows.push({
      label: "Current",
      rawLabel: "",
      equity: Number(currentMetrics.equity || 0),
      pnl: Number(currentMetrics.realized_pnl || 0) + Number(currentMetrics.unrealized_pnl || 0),
    })
  }

  state.history = rows
}

async function refresh() {
  const payload = await request("/api/status")
  const trades = await request("/api/trades")
  const strategies = await request("/api/strategies")
  const history = await request("/api/history?limit=2000")
  const exchange = await request("/api/exchange")
  state.latestStatus = payload
  state.latestStrategies = strategies
  state.latestExchange = exchange
  updateHistoryFromSnapshots(history.snapshots || [], payload.metrics || {})
  renderMetrics(payload.metrics || {})
  renderHeroControls()
  renderHistorySummary()
  renderBotState(payload)
  renderExchangeOverview(exchange)
  renderPositions(payload.positions || [])
  renderTrades(trades.trades || [])
  renderStrategies(strategies)
  renderBacktestControls()
  renderBacktestResult()
  renderSignals(payload)
  updateChart(payload.metrics || {})
}

async function runBacktestFromDashboard() {
  if (state.backtestLoading) {
    return
  }

  captureBacktestFormFromDom()
  const profile = state.backtestForm.profile || "default"
  const compareText = state.backtestForm.compareText || ""
  const symbolsText = state.backtestForm.symbolsText || ""
  const interval = state.backtestForm.interval || "5m"
  const candlesLimit = Number(state.backtestForm.candlesLimit || 200)
  const leverage = Number(state.backtestForm.leverage || 3)

  state.backtestLoading = true
  state.backtestError = ""
  renderBacktestControls()
  renderBacktestResult()

  const compare = compareText
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
  const symbols = symbolsText
    .split(",")
    .map((item) => item.trim().toUpperCase())
    .filter(Boolean)

  const payload = {
    profile,
    symbols,
    interval,
    candles_limit: candlesLimit,
    leverage,
  }
  if (compare.length) {
    payload.compare = compare
  }

  try {
    const result = await request("/api/backtest/run", {
      method: "POST",
      body: JSON.stringify(payload),
    })
    state.backtestResult = result
    state.backtestError = ""
  } catch (error) {
    state.backtestResult = null
    state.backtestError = parseApiError(error.message || "Backtest request failed")
  } finally {
    state.backtestLoading = false
  }
  renderBacktestControls()
  renderBacktestResult()
}

async function loadStrategyFromDashboard() {
  const status = state.latestStatus?.state || {}
  if (status.running) {
    throw new Error("Stop the bot before changing strategy profile")
  }
  const selected = document.getElementById("strategyProfileSelect")?.value || ""
  if (!selected) {
    throw new Error("No saved strategy profile selected")
  }
  await request(`/api/strategies/load/${encodeURIComponent(selected)}`, { method: "POST" })
  await refresh()
}

document.addEventListener("click", async (event) => {
  const target = event.target
  if (!(target instanceof HTMLElement)) {
    return
  }
  const closeSymbol = target.dataset.closeSymbol
  if (closeSymbol) {
    await request(`/api/trades/${closeSymbol}/close`, { method: "POST" })
    await refresh()
    return
  }
  const action = target.dataset.action
  if (!action) {
    return
  }
  const endpointMap = {
    start: "/api/start",
    stop: "/api/stop",
    pause: "/api/pause",
    resume: "/api/resume",
    "run-once": "/api/run-once",
    "seed-strategy": "/api/seed-default-strategy",
  }
  if (endpointMap[action]) {
    await request(endpointMap[action], { method: "POST" })
    await refresh()
    return
  }

  if (action === "run-backtest") {
    await runBacktestFromDashboard()
    return
  }

  if (action === "load-strategy") {
    try {
      await loadStrategyFromDashboard()
    } catch (error) {
      document.body.insertAdjacentHTML(
        "afterbegin",
        `<div style="position:fixed;top:12px;left:12px;padding:12px 16px;background:#7f1d1d;color:#fff;border-radius:12px;z-index:1000;">${error.message}</div>`,
      )
    }
  }
})

document.addEventListener("input", (event) => {
  const target = event.target
  if (!(target instanceof HTMLElement)) {
    return
  }
  if (
    target.id === "backtestProfile" ||
    target.id === "backtestCompare" ||
    target.id === "backtestSymbols" ||
    target.id === "backtestInterval" ||
    target.id === "backtestCandlesLimit" ||
    target.id === "backtestLeverage"
  ) {
    captureBacktestFormFromDom()
  }
})

document.addEventListener("change", (event) => {
  const target = event.target
  if (!(target instanceof HTMLElement)) {
    return
  }
  if (target.id === "backtestProfile") {
    captureBacktestFormFromDom()
  }
})

refresh().catch((error) => {
  console.error(error)
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div style="position:fixed;top:12px;left:12px;padding:12px 16px;background:#7f1d1d;color:#fff;border-radius:12px;z-index:1000;">${error.message}</div>`,
  )
})
setInterval(() => renderHeroControls(), 1000)
setInterval(() => refresh().catch(console.error), 15000)
