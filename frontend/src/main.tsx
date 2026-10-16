import { FormEvent, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { AssetPicker, assetLabel } from "./AssetPicker";
import { ResearchResults, type ResearchDetail as Detail, type ResearchRun as Run } from "./ResearchResults";
import "./style.css";

type Scalar = string | number;
type SymbolSetting = { symbol: string; enabled: boolean; leverage: number };
type LeverageSchedule = {
  symbol: string; maximum_leverage: number; fetched_at: string;
  brackets: { initial_leverage: number; notional_floor: string; notional_cap: string }[];
};
type Risk = { quote_asset: "USDC" | "USDT"; policy: Record<string, Scalar>; symbols: SymbolSetting[] };
type Config<T> = { version: number; configuration: T | null; defaults: T | null };
type Auth = { authenticated: boolean; username: string | null; csrf_token: string; expires_at: string };
type Audit = { id: string; actor: string; action: string; created_at: string; details: unknown };
type PaperRun = { id: string; version: number; status: string; symbol: string; state: Record<string, unknown>; configuration: unknown; updated_at: string };
type PaperCapabilities = { available: boolean; note: string; simulation_defaults: Record<string, string> };

const suggestedRisk: Risk = {
  quote_asset: "USDC",
  policy: {
    basis: "available_balance",
    risk_fraction: "0.005",
    margin_reserve_fraction: "0.2",
    maximum_position_notional: "1000",
    maximum_total_exposure: "3000",
    maximum_aggregate_risk: "30",
    maximum_daily_loss: "50",
    maximum_drawdown_fraction: "0.1",
    maximum_stop_distance_fraction: "0.05",
    minimum_reward_to_risk: "2",
    maximum_open_positions: 3,
    maximum_account_age: "PT15S",
    maximum_market_age: "PT15S",
  },
  symbols: [{ symbol: "BTCUSDC", enabled: true, leverage: 1 }],
};
const label = (name: string) => name.replaceAll("_", " ");

function App() {
  const [auth, setAuth] = useState<Auth | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("settings");
  const [version, setVersion] = useState(0);
  const [risk, setRisk] = useState<Risk>(suggestedRisk);
  const [leverageSchedules, setLeverageSchedules] = useState<LeverageSchedule[]>([]);
  const [leverageError, setLeverageError] = useState("");
  const [strategy, setStrategy] = useState<Record<string, Scalar>>({});
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  const [initialized, setInitialized] = useState({ risk: false, strategy: false });
  const [audits, setAudits] = useState<Audit[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [paper, setPaper] = useState<PaperCapabilities | null>(null);
  const [paperRuns, setPaperRuns] = useState<PaperRun[]>([]);
  const [paperEvents, setPaperEvents] = useState<unknown>(null);
  const [simulation, setSimulation] = useState<Record<string, string>>({});
  const [paperSymbol, setPaperSymbol] = useState("");
  const [paperEnvironment, setPaperEnvironment] = useState("testnet");
  const [paperStart, setPaperStart] = useState(new Date(Math.floor(Date.now() / 900000) * 900000).toISOString());
  const [researchStart, setResearchStart] = useState(new Date(Math.floor(Date.now() / 900000) * 900000 - 86400000).toISOString());
  const [researchEnd, setResearchEnd] = useState(new Date(Math.floor(Date.now() / 900000) * 900000 - 900000).toISOString());
  const [researchSymbol, setResearchSymbol] = useState("");
  const [researchSimulation, setResearchSimulation] = useState<Record<string, string>>({});
  const [offset, setOffset] = useState(0);

  async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(method !== "GET" ? { "X-CSRF-Token": auth?.csrf_token ?? "" } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/auth/login") {
        setAuth(null);
        const fresh = await fetch("/api/auth/session", { credentials: "same-origin" });
        setAuth(await fresh.json());
      }
      throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    }
    return data as T;
  }

  async function loadSettings() {
    setSettingsLoaded(false);
    setLeverageSchedules([]); setLeverageError("");
    const [r, s] = await Promise.all([
      api<Config<Risk>>("/api/config/risk"),
      api<Config<Record<string, Scalar>>>("/api/config/strategy"),
    ]);
    if (r.version !== s.version) throw new Error("Settings changed while loading. Reload settings.");
    setVersion(r.version);
    setRisk(r.configuration ?? structuredClone(suggestedRisk));
    setStrategy({ ...s.defaults, ...s.configuration });
    setInitialized({ risk: r.configuration !== null, strategy: s.configuration !== null });
    try {
      const leverage = await api<{ items: LeverageSchedule[] }>("/api/market/leverage");
      setLeverageSchedules(leverage.items);
    } catch (failure) {
      setLeverageError(failure instanceof Error ? failure.message : "Binance leverage verification failed.");
    }
    setSettingsLoaded(true);
  }

  async function loadPaper() {
    const [capabilities, runs, savedRisk] = await Promise.all([
      api<PaperCapabilities>("/api/paper/capabilities"),
      api<{ items: PaperRun[] }>("/api/paper/runs"),
      api<Config<Risk>>("/api/config/risk"),
    ]);
    setPaper(capabilities); setPaperRuns(runs.items);
    setSimulation(previous => Object.keys(previous).length ? previous : capabilities.simulation_defaults);
    const saved = savedRisk.configuration;
    setRisk(saved ?? { ...structuredClone(suggestedRisk), symbols: [] });
    setPaperSymbol(previous => saved?.symbols.some(s => s.enabled && s.symbol === previous)
      ? previous : saved?.symbols.find(s => s.enabled)?.symbol ?? "");
  }

  async function controlPaper(run: PaperRun, action: string) {
    if (!confirm(`Confirm paper ${action} for ${run.symbol}? This affects simulated orders only. Stopping with an open position waits for the worker to close it.`)) return;
    await api(`/api/paper/runs/${encodeURIComponent(run.id)}/control`, "POST", { expected_version: run.version, action });
    await loadPaper();
    setMessage(`Paper ${action} accepted. Live trading remains disabled.`);
  }

  async function attempt(work: () => Promise<void>) {
    setBusy(true); setError(""); setMessage("");
    try { await work(); } catch (e) { setError(e instanceof Error ? e.message : "Request failed"); }
    finally { setBusy(false); }
  }

  useEffect(() => { void attempt(async () => setAuth(await api<Auth>("/api/auth/session"))); }, []);
  useEffect(() => {
    if (!auth?.authenticated) return;
    void attempt(async () => {
      if (tab === "settings") await loadSettings();
      if (tab === "audit") setAudits((await api<{ items: Audit[] }>(`/api/audit?limit=100&offset=${offset}`)).items);
      if (tab === "research") {
        const [results, savedRisk, capabilities] = await Promise.all([
          api<{ items: Run[] }>(`/api/research?limit=100&offset=${offset}`),
          api<Config<Risk>>("/api/config/risk"),
          api<PaperCapabilities>("/api/paper/capabilities"),
        ]);
        setRuns(results.items);
        setResearchSimulation(previous => Object.keys(previous).length ? previous : capabilities.simulation_defaults);
        const saved = savedRisk.configuration;
        setRisk(saved ?? { ...structuredClone(suggestedRisk), symbols: [] });
        setResearchSymbol(previous => saved?.symbols.some(s => s.enabled && s.symbol === previous)
          ? previous : saved?.symbols.find(s => s.enabled)?.symbol ?? "");
      }
      if (tab === "paper") await loadPaper();
    });
  }, [auth?.authenticated, tab, offset]);

  function login(event: FormEvent) {
    event.preventDefault();
    setSettingsLoaded(false);
    void attempt(async () => {
      try { setAuth(await api<Auth>("/api/auth/login", "POST", { username, password })); }
      finally { setPassword(""); }
    });
  }

  function save(event: FormEvent, kind: "risk" | "strategy") {
    event.preventDefault();
    if (!settingsLoaded || (kind === "strategy" && Object.keys(strategy).length === 0)) return;
    void attempt(async () => {
      if (kind === "risk" && risk.symbols.length === 0) throw new Error("Choose at least one asset before saving.");
      if (kind === "risk" && risk.symbols.some(s => !leverageSchedules.some(b => b.symbol === s.symbol))) {
        throw new Error("Verify Binance leverage brackets for every selected asset before saving.");
      }
      await api(`/api/config/${kind}`, "PUT", {
        expected_version: version, configuration: kind === "risk" ? risk : strategy,
      });
      await loadSettings();
      setMessage(`${label(kind)} settings saved. Both forms reloaded at the current shared version. Live trading remains disabled.`);
    });
  }

  const changeSymbol = (index: number, update: Partial<SymbolSetting>) =>
    setRisk({ ...risk, symbols: risk.symbols.map((symbol, i) => i === index ? { ...symbol, ...update } : symbol) });

  return <main>
    <header><div className="brand"><img className="app-icon" src="/icon.svg" alt="" width="56" height="56" /><div><h1>Futures Operator</h1><p>Research &amp; paper operations</p></div></div>
      <strong className="badge">LIVE TRADING DISABLED</strong>
      {auth?.authenticated && <button disabled={busy} onClick={() => void attempt(async () => {
        await api("/api/auth/logout", "POST", {});
        setAuth(await api<Auth>("/api/auth/session"));
        setDetail(null); setAudits([]); setRuns([]);
      })}>Log out {auth.username}</button>}
    </header>
    {error && <div role="alert" className="error">{error}</div>}
    {message && <div role="status" className="success">{message}</div>}
    {!auth?.authenticated ? <section className="login">
      <h2>Owner sign in</h2><p>Owner credentials are created locally by the bootstrap command. There is no public registration.</p>
      <form onSubmit={login}>
        <label>Username<input autoComplete="username" value={username} maxLength={64} required onChange={e => setUsername(e.target.value)} /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} maxLength={1024} required onChange={e => setPassword(e.target.value)} /></label>
        <button disabled={busy || !auth}>Sign in</button>
      </form>
    </section> : <>
      <nav aria-label="Dashboard sections">{["settings", "audit", "research", "paper"].map(name =>
        <button key={name} aria-current={tab === name ? "page" : undefined} onClick={() => {
          if (name === "settings" && tab !== "settings") setSettingsLoaded(false);
          setTab(name); setOffset(0);
        }}>{label(name)}</button>)}</nav>
      {tab === "settings" && <>
        {!settingsLoaded && <p role="status">Settings are not ready yet. Loading saved settings and strategy defaults; saves remain disabled until loading succeeds.</p>}
        <div className="toolbar"><p>Shared configuration version: <b>{version}</b>. Saving either form reloads both. Stale writes are rejected.</p>
          <button disabled={busy} onClick={() => void attempt(loadSettings)}>Reload settings</button></div>
        <div className="columns">
          <section><h2>Risk &amp; assets</h2><p>{!settingsLoaded ? "Loading risk settings…" : initialized.risk ? "Saved configuration" : "Not initialized. These are suggestions only; review every limit before saving."}</p>
            <form onSubmit={event => save(event, "risk")}><fieldset disabled={busy || !settingsLoaded}>
              <label>Quote asset<select value={risk.quote_asset} onChange={e => {
                const quote = e.target.value as Risk["quote_asset"];
                if (risk.symbols.length && !confirm(`Switch to ${quote}? This clears the asset selections in this unsaved form. Choose verified ${quote} contracts; saved settings and existing runs are unchanged until you save.`)) return;
                setRisk({ ...risk, quote_asset: quote, symbols: [] });
              }}><option>USDC</option><option>USDT</option></select></label>
              <p>Decimal limits use exact strings. Fractions are 0–1, not percentages. Set leverage independently for each asset, up to your Binance production account’s verified maximum. Larger positions have lower leverage limits; simulation sizing also respects the matching notional bracket. Higher leverage increases liquidation risk, not the stop-loss budget.</p>
              <p>Risk fraction is the account loss budget at SL, not margin or position size. For example, 0.005 means at most 5 USDC of modeled stop loss on a 1,000 USDC selected balance/equity basis. New runs include entry/exit fees and expected stop slippage by default, keep the structural SL unchanged, and round quantity down. Safety caps may use less of that budget; gaps, funding and liquidation can still exceed it.</p>
              {leverageError && <p role="alert" className="error">{leverageError} Leverage controls and risk saves need verified brackets. Use Reload settings to retry; existing selections are unchanged.</p>}
              <p>Bracket limits are a current production-account snapshot, not historical guarantees or permission to trade live. New runs refresh the snapshot; existing runs retain their recorded limits.</p>
              {Object.entries(risk.policy).filter(([key]) => key !== "maximum_leverage").map(([key, value]) => <label key={key}>{key === "maximum_account_age" ? "Maximum balance-data age (ISO duration)" : key === "maximum_market_age" ? "Maximum price-data age (ISO duration)" : label(key)}
                {key === "basis" ? <select value={value} onChange={e => setRisk({ ...risk, policy: { ...risk.policy, [key]: e.target.value } })}>
                  <option value="available_balance">available balance</option><option value="equity">equity</option>
                </select> : <input required type={typeof value === "number" ? "number" : "text"} step="1"
                  value={value} onChange={e => setRisk({ ...risk, policy: { ...risk.policy, [key]: typeof value === "number" ? Number(e.target.value) : e.target.value } })} />}
                {key === "maximum_account_age" && <small>How old a balance/equity snapshot may be before new entries are blocked. Not the age of your Binance account. PT15S means 15 seconds. Simulation creates a balance snapshot for each bar.</small>}
                {key === "maximum_market_age" && <small>How old the price snapshot may be before new entries are blocked. PT15S means 15 seconds; PT1M means 1 minute. Prevents sizing from stale prices; this is not the candle interval or holding time.</small>}
              </label>)}
              <AssetPicker quote={risk.quote_asset} selected={risk.symbols.map(s => s.symbol)} onAdd={symbol => setRisk(previous =>
                previous.symbols.some(item => item.symbol === symbol) ? previous : { ...previous, symbols: [...previous.symbols, { symbol, leverage: 1, enabled: true }] }
              )} />
              <h3>Selected assets ({risk.symbols.length})</h3>
              <p>Enable an asset to make it available for new research and paper runs. There is no separate portfolio leverage limit. Existing run snapshots do not change.</p>
              {risk.symbols.length === 0 && <p>Select at least one asset from the directory above.</p>}
              {risk.symbols.map((s, index) => {
                const schedule = leverageSchedules.find(item => item.symbol === s.symbol);
                const eligible = schedule?.brackets.filter(b => b.initial_leverage >= s.leverage);
                const cap = eligible?.length ? eligible[eligible.length - 1].notional_cap : null;
                return <div className="symbol" key={s.symbol}>
                <div><strong>{assetLabel(s.symbol, risk.quote_asset)}</strong><small>{s.symbol} · Perpetual</small></div>
                <label>Trade leverage (×)<input aria-label={`Leverage for ${s.symbol}`} type="number" required min={1} max={schedule?.maximum_leverage} disabled={!schedule} step={1} value={s.leverage} onChange={e => changeSymbol(index, { leverage: Number(e.target.value) })} />
                  <small>{schedule ? `Binance maximum ${schedule.maximum_leverage}× · ${s.leverage}× notional cap: ${cap ?? "not allowed"} ${risk.quote_asset} · verified ${new Date(schedule.fetched_at).toLocaleString()}` : "No verified Binance brackets for this asset. Reload settings or remove it; saving is blocked."}</small>
                </label>
                <label className="check"><input type="checkbox" checked={s.enabled} onChange={e => changeSymbol(index, { enabled: e.target.checked })} />Enabled</label>
                <button type="button" aria-label={`Remove ${assetLabel(s.symbol, risk.quote_asset)}`} onClick={() => setRisk({ ...risk, symbols: risk.symbols.filter((_, i) => i !== index) })}>Remove</button>
              </div>; })}
              <button type="submit" disabled={risk.symbols.length === 0 || risk.symbols.some(s => !leverageSchedules.some(b => b.symbol === s.symbol))}>Save risk settings</button>
            </fieldset></form>
          </section>
          <section><h2>Strategy parameters</h2><p>{!settingsLoaded ? "Loading strategy settings and implementation defaults…" : initialized.strategy ? "Saved configuration" : "Not initialized. Implementation defaults shown; save explicitly to initialize."}</p>
            <p>New research and paper runs use confirmed 15-minute and hourly swing support/resistance by default. Stops sit one tick beyond the nearest protective level; targets sit one tick before a qualifying opposing level. The minimum ratio is the larger of the strategy target ratio and the risk minimum, after expected entry slippage. The optional experimental cost filter also checks exit slippage and both trading fees; it is off by default and does not guarantee future funding or gaps. No suitable levels means no trade. Positions have no automatic time limit: SL/TP, liquidation and explicit stop controls still apply. Historical backtests close remaining positions at the dataset boundary for accounting, not because a holding timer expired. Previously created paper runs keep their original snapshots.</p>
            <form onSubmit={event => save(event, "strategy")}><fieldset disabled={busy || !settingsLoaded || Object.keys(strategy).length === 0}>
              {Object.entries(strategy).filter(([key]) => !["time_stop_bars", "exit_policy"].includes(key) && (strategy.protection_mode === "legacy_atr" || !["atr_period", "atr_multiplier", "swing_period"].includes(key))).map(([key, value]) => <label key={key}>{key === "reward_multiple" ? "Minimum target reward / risk" : label(key)}{key === "cost_filter" ? <select value={value} onChange={e => setStrategy({ ...strategy, [key]: e.target.value })}>
                <option value="gross_only">Gross price ratio after entry slippage (default)</option>
                <option value="net_reward_risk">Experimental: reward/risk after fees and exit slippage</option>
              </select> : key === "volume_filter" ? <select value={value} onChange={e => setStrategy({ ...strategy, [key]: e.target.value })}>
                <option value="signal_above_prior_mean">Signal volume ≥ prior 20 closed bars' mean (default)</option>
                <option value="disabled">Disabled (comparison / legacy)</option>
              </select> : key === "protection_mode" ? <select value={value} onChange={e => setStrategy({ ...strategy, [key]: e.target.value })}>
                <option value="support_resistance">Confirmed support / resistance</option>
                <option value="legacy_atr">Legacy ATR / exact-R (compatibility only)</option>
              </select> : <input required
                type={typeof value === "number" ? "number" : "text"} step="1" value={value}
                onChange={e => setStrategy({ ...strategy, [key]: typeof value === "number" ? Number(e.target.value) : e.target.value })} />}
                {key === "level_lookback_bars" && <small>Closed bars searched independently on each timeframe; 96 means 24 hours of 15m levels and 96 hours of 1h levels. Broken levels are excluded.</small>}
                {key === "pivot_confirmation_bars" && <small>Closed candles required on each side of a strict swing high/low. A level cannot be used before its right-hand candles close.</small>}
                {key === "volume_filter" && <small>Signal-bar base-asset volume must be at least the mean of the preceding 20 closed entry bars, excluding the signal. Applies to new runs only; existing paper snapshots are unchanged.</small>}
              </label>)}
              <button type="submit">Save strategy settings</button>
            </fieldset></form>
            <p>Saving parameters does not place orders or enable live trading. Research results never automatically promote settings.</p>
          </section>
        </div>
      </>}
      {tab === "audit" && <section><h2>Audit trail</h2><p>Newest first; up to 100 events per page.</p>
        {audits.length === 0 ? <p>No events on this page.</p> : <div className="scroll"><table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Details</th></tr></thead><tbody>
          {audits.map(a => <tr key={a.id}><td>{a.created_at}</td><td>{a.actor}</td><td>{a.action}</td><td><pre>{JSON.stringify(a.details, null, 2)}</pre></td></tr>)}
        </tbody></table></div>}
      </section>}
      {tab === "research" && <section><h2>Research runs</h2>
        {!risk.symbols.some(s => s.enabled) && <p>No saved enabled assets. Review and save assets in Settings before starting research.</p>}
        <details><summary>Run public-data research</summary><p>Uses saved risk and strategy snapshots with public production market data. No exchange orders or setting promotion. Range: 15 minutes–30 days; timestamps must be UTC ISO values aligned to 15-minute opens. This may take several minutes; leave this page open while it runs.</p>
          <form onSubmit={e => { e.preventDefault(); void attempt(async () => {
            const result = await api<{ id: string; result: unknown }>("/api/research/run", "POST", {
              symbol: researchSymbol, start: researchStart, end: researchEnd, simulation: researchSimulation,
            });
            setRuns((await api<{ items: Run[] }>("/api/research?limit=100&offset=0")).items);
            setOffset(0);
            setDetail(await api<Detail>(`/api/research/${encodeURIComponent(result.id)}`));
            setMessage(`Research ${result.id} completed and persisted. No live orders were submitted.`);
          }); }}><fieldset disabled={busy}>
            <label>Enabled saved symbol<select required value={researchSymbol} onChange={e => setResearchSymbol(e.target.value)}>
              <option value="">Select saved enabled symbol</option>
              {risk.symbols.filter(s => s.enabled).map(s => <option key={s.symbol} value={s.symbol}>{assetLabel(s.symbol, risk.quote_asset)} · {s.leverage}×</option>)}
            </select></label>
            <label>Start (UTC ISO timestamp)<input required value={researchStart} onChange={e => setResearchStart(e.target.value)} /></label>
            <label>End (exclusive UTC ISO timestamp)<input required value={researchEnd} onChange={e => setResearchEnd(e.target.value)} /></label>
            {Object.entries(researchSimulation).map(([key, value]) => <label key={key}>{label(key)}
              {key === "stop_risk_model" ? <select value={value} onChange={e => setResearchSimulation({ ...researchSimulation, [key]: e.target.value })}>
                <option value="fees_and_slippage">Total SL loss including fees and stop slippage (default)</option>
                <option value="price_only">Legacy: price loss only (comparison)</option>
              </select> : <input required value={value} onChange={e => setResearchSimulation({ ...researchSimulation, [key]: e.target.value })} />}
            </label>)}
            <button type="submit">Run and save research</button>
          </fieldset></form>
        </details>
        <p>Position quantity uses your saved account-risk fraction divided by modeled loss per unit at SL, including both trading fees and expected adverse stop slippage by default. Notional, margin and Binance limits can reduce actual risk below the requested percentage; entry sizing details are recorded in each new trade. Gaps, liquidation and funding can exceed the estimate.</p>
        <p>Persisted results below are read-only. CSV exports include holdout trades for validation runs.</p>
        <ResearchResults runs={runs} detail={detail} busy={busy}
          inspect={id => void attempt(async () => setDetail(await api<Detail>(`/api/research/${encodeURIComponent(id)}`)))}
          close={() => setDetail(null)} />
      </section>}
      {(tab === "audit" || tab === "research") && <div className="toolbar">
        <button disabled={busy || offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</button>
        <span>Page {offset / 100 + 1}</span>
        <button disabled={busy || (tab === "audit" ? audits.length : runs.length) < 100} onClick={() => setOffset(offset + 100)}>Next</button>
        <button disabled={busy} onClick={() => void attempt(async () => {
          if (tab === "audit") setAudits((await api<{ items: Audit[] }>(`/api/audit?limit=100&offset=${offset}`)).items);
          else setRuns((await api<{ items: Run[] }>(`/api/research?limit=100&offset=${offset}`)).items);
        })}>Refresh</button>
      </div>}
      {tab === "paper" && <section><h2>Paper engine</h2>
        {!risk.symbols.some(s => s.enabled) && <p>No saved enabled assets. Review and save assets in Settings before creating a run.</p>}
        <p>{paper?.note}</p><p>Only persisted simulation state is displayed. Production below means public production <em>market data</em>, never live orders. A separate worker must be running for progress.</p>
        <button disabled={busy} onClick={() => void attempt(loadPaper)}>Refresh paper state</button>
        <div className="columns"><article><h3>Create paused paper run</h3><p>Requires saved strategy and risk settings. A run snapshots both; later setting changes do not affect it. Only one active paper portfolio is allowed.</p>
          <form onSubmit={e => { e.preventDefault(); void attempt(async () => {
            await api("/api/paper/runs", "POST", { symbol: paperSymbol, environment: paperEnvironment, start: paperStart, simulation });
            await loadPaper(); setMessage("Paper run created paused. Inspect its configuration, then resume explicitly.");
          }); }}><fieldset disabled={busy || !paper?.available}>
            <label>Enabled symbol<select value={paperSymbol} required onChange={e => setPaperSymbol(e.target.value)}>
              <option value="">Select saved enabled symbol</option>
              {risk.symbols.filter(s => s.enabled).map(s => <option key={s.symbol} value={s.symbol}>{assetLabel(s.symbol, risk.quote_asset)} · {s.leverage}×</option>)}
            </select></label>
            <label>Market data environment<select value={paperEnvironment} onChange={e => setPaperEnvironment(e.target.value)}>
              <option value="testnet">Testnet market data</option><option value="production">Production public market data (paper only)</option>
            </select></label>
            <label>First 15-minute bar (UTC ISO timestamp)<input required value={paperStart} onChange={e => setPaperStart(e.target.value)} /></label>
            {Object.entries(simulation).map(([key, value]) => <label key={key}>{label(key)}{key === "stop_risk_model" ? <select value={value} onChange={e => setSimulation({ ...simulation, [key]: e.target.value })}>
              <option value="fees_and_slippage">Total SL loss including fees and stop slippage (default)</option>
              <option value="price_only">Legacy: price loss only (comparison)</option>
            </select> : <input required value={value} onChange={e => setSimulation({ ...simulation, [key]: e.target.value })} />}</label>)}
            <button type="submit">Create paused simulation</button>
          </fieldset></form>
        </article><article><h3>Persisted paper portfolios</h3>
          {paperRuns.length === 0 && <p>No paper runs recorded.</p>}
          {paperRuns.map(run => <section key={run.id}><h3>{run.symbol} · {run.status}</h3><p>Version {run.version} · Updated {run.updated_at}</p>
            <pre>{JSON.stringify(run.state, null, 2)}</pre>
            <div className="paper-controls">
              <button disabled={busy || run.status !== "paused"} onClick={() => void attempt(() => controlPaper(run, "resume"))}>Resume paper</button>
              <button disabled={busy || run.status !== "running"} onClick={() => void attempt(() => controlPaper(run, "pause"))}>Pause paper</button>
              <button disabled={busy || run.status === "stopped" || run.status === "stopping"} onClick={() => void attempt(() => controlPaper(run, "stop"))}>Stop paper</button>
              <button disabled={busy || run.status !== "faulted"} onClick={() => void attempt(() => controlPaper(run, "recover"))}>Acknowledge fault</button>
              <button disabled={busy} onClick={() => void attempt(async () => setPaperEvents(await api(`/api/paper/runs/${encodeURIComponent(run.id)}/events`)))}>Load events</button>
            </div><details><summary>Immutable configuration snapshot</summary><pre>{JSON.stringify(run.configuration, null, 2)}</pre></details>
          </section>)}
          {paperEvents !== null && <article><h3>Selected run events (latest 100)</h3><pre>{JSON.stringify(paperEvents, null, 2)}</pre><button onClick={() => setPaperEvents(null)}>Close events</button></article>}
        </article></div>
      </section>}
    </>}
    <footer>Same-origin operator console · No exchange credentials are displayed · {busy || (auth?.authenticated && tab === "settings" && !settingsLoaded) ? "Working…" : "Ready"}</footer>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
