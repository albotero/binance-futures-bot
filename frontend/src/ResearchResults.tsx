import { useEffect, useRef, useState } from "react";

type Numeric = string | number | null;
type Trade = {
  symbol?: string; direction?: string; entry_time?: string; exit_time?: string;
  entry_price?: Numeric; exit_price?: Numeric; quantity?: Numeric;
  net_pnl?: Numeric; fees?: Numeric; funding_pnl?: Numeric; exit_reason?: string;
  stop_price?: Numeric; target_price?: Numeric;
};
export type ResearchMetrics = {
  symbol?: string; quote_asset?: string; start?: string; end?: string;
  starting_balance?: Numeric; final_equity?: Numeric; net_pnl?: Numeric;
  total_fees?: Numeric; funding_pnl?: Numeric; maximum_drawdown_fraction?: Numeric;
  win_rate?: Numeric; profit_factor?: Numeric; expectancy?: Numeric; halted?: boolean;
  trade_count?: number | null; scope?: string;
  trades?: Trade[]; equity_curve?: { time: string; equity: Numeric }[];
  strategy_observations?: Record<string, number>; risk_rejections?: unknown[];
  limitations?: string[];
};
export type ResearchRun = {
  id: string; status: string; created_at: string; strategy_version?: string;
  dataset_metadata: Record<string, unknown>; summary?: ResearchMetrics;
};
export type ResearchDetail = {
  id: string; status?: string; strategy_version?: string;
  metrics: (ResearchMetrics & {
    selected_holdout?: ResearchMetrics | null; baseline_holdout?: ResearchMetrics;
    selected_checksum?: string | null;
  }) | null;
  configuration_snapshot: unknown; dataset_metadata: Record<string, unknown>;
};

const number = (value: Numeric | undefined) => {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};
const format = (value: Numeric | undefined, digits = 2, minimumDigits = digits) => {
  const parsed = number(value);
  return parsed === null ? "Not recorded" : parsed.toLocaleString(undefined, {
    minimumFractionDigits: minimumDigits, maximumFractionDigits: digits,
  });
};
const signed = (value: Numeric | undefined) => {
  const parsed = number(value);
  return `${parsed !== null && parsed > 0 ? "+" : ""}${format(value)}`;
};
const percent = (value: Numeric | undefined) => {
  const parsed = number(value);
  return parsed === null ? "Not recorded" : `${format(parsed * 100)}%`;
};
const tone = (value: Numeric | undefined) => {
  const parsed = number(value);
  return parsed === null || parsed === 0 ? "" : parsed > 0 ? "positive" : "negative";
};
const date = (value: unknown) => {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return "Not recorded";
  return new Date(value).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    timeZone: "UTC",
  });
};
const text = (value: unknown) => typeof value === "string" ? value : "Not recorded";
const scopeLabel = (scope?: string) => scope === "selected_holdout" ? "Selected holdout"
  : scope === "baseline_holdout" ? "Baseline holdout · no selected candidate" : "Historical backtest";
const csvUrl = (id: string) => `/api/research/${encodeURIComponent(id)}/trades.csv`;

function Metric({ title, value, note, className = "" }: {
  title: string; value: string; note?: string; className?: string;
}) {
  return <div className="research-metric"><dt>{title}</dt>
    <dd className={className}>{value}</dd>{note && <small>{note}</small>}</div>;
}

function Performance({ metrics, compact = false }: { metrics: ResearchMetrics; compact?: boolean }) {
  const count = metrics.trade_count ?? metrics.trades?.length;
  const balance = number(metrics.starting_balance);
  const pnl = number(metrics.net_pnl);
  const returnFraction = balance !== null && balance > 0 && pnl !== null ? pnl / balance : null;
  return <dl className={`research-metrics${compact ? " compact" : ""}`}>
    <Metric title="Net PNL" value={signed(metrics.net_pnl)} className={tone(metrics.net_pnl)}
      note={`${metrics.quote_asset ?? "Quote not recorded"} · ${percent(returnFraction)} return`} />
    <Metric title="Max drawdown" value={percent(metrics.maximum_drawdown_fraction)} />
    <Metric title="Win rate" value={percent(metrics.win_rate)} note={`${count ?? "Unknown"} completed trades`} />
    <Metric title="Profit factor" value={metrics.profit_factor === null
      ? (count ? "No losing trades" : "Not applicable") : format(metrics.profit_factor)} />
    {!compact && <>
      <Metric title="Final equity" value={format(metrics.final_equity)}
        note={`Started at ${format(metrics.starting_balance)} ${metrics.quote_asset ?? ""}`} />
      <Metric title="Trading fees" value={format(metrics.total_fees)} note="Both entry and exit" />
      <Metric title="Funding PNL" value={signed(metrics.funding_pnl)} className={tone(metrics.funding_pnl)} />
      <Metric title="Expectancy / trade" value={signed(metrics.expectancy)} className={tone(metrics.expectancy)} />
    </>}
  </dl>;
}

function EquityChart({ metrics }: { metrics: ResearchMetrics }) {
  const points = (metrics.equity_curve ?? []).flatMap(point => {
    const equity = number(point.equity);
    const time = Date.parse(point.time);
    return equity !== null && Number.isFinite(time) ? [{ equity, time }] : [];
  });
  if (points.length < 2) return <p className="research-empty">Equity curve not recorded for this run.</p>;
  const min = Math.min(...points.map(p => p.equity));
  const max = Math.max(...points.map(p => p.equity));
  const padding = Math.max((max - min) * 0.12, Math.abs(max) * 0.001, 0.01);
  const low = min - padding;
  const high = max + padding;
  const first = points[0];
  const last = points[points.length - 1];
  const x = (time: number) => 80 + (time - first.time) / (last.time - first.time || 1) * 890;
  const y = (equity: number) => 30 + (high - equity) / (high - low) * 200;
  const line = points.map(p => `${x(p.time)},${y(p.equity)}`).join(" ");
  return <figure className="equity-chart">
    <figcaption><strong>Account equity</strong><span>{metrics.quote_asset ?? "Quote not recorded"} · sampled at bar boundaries</span></figcaption>
    <svg viewBox="0 0 1000 270" role="img" aria-label={`Equity from ${format(first.equity)} to ${format(last.equity)} ${metrics.quote_asset ?? ""}`}>
      {[low, (low + high) / 2, high].map(value => <g key={value}>
        <line x1="80" x2="970" y1={y(value)} y2={y(value)} className="chart-grid" />
        <text x="68" y={y(value) + 4} textAnchor="end">{format(value)}</text>
      </g>)}
      <polygon points={`80,230 ${line} 970,230`} className="chart-area" />
      <polyline points={line} className={`chart-line ${last.equity >= first.equity ? "positive" : "negative"}`} />
      <text x="80" y="258">{date(new Date(first.time).toISOString())} UTC</text>
      <text x="970" y="258" textAnchor="end">{date(new Date(last.time).toISOString())} UTC</text>
    </svg>
    <p className="chart-caption">Bar-boundary equity is not intrabar drawdown. Dataset-end closures settle historical accounting, not a holding-time rule.</p>
  </figure>;
}

function TradeLedger({ trades }: { trades: Trade[] }) {
  const [page, setPage] = useState(0);
  const size = 10;
  return <div className="trade-ledger">
    <div className="research-heading"><div><h4>Completed trades</h4>
      <small>All times UTC · amounts in the run's quote asset</small></div>
      <span className="research-pill">{trades.length} trades</span></div>
    {trades.length === 0 ? <p className="research-empty">No completed trades. Review the strategy evaluation reasons below to understand why.</p> :
      <><div className="scroll" tabIndex={0} role="region" aria-label="Completed trades table">
        <table><thead><tr><th>Side / asset</th><th>Entry / exit (UTC)</th><th>Entry → exit price</th><th>Quantity</th><th>SL / TP</th><th>Net PNL</th><th>Fees / funding</th><th>Exit</th></tr></thead>
          <tbody>{trades.slice(page * size, (page + 1) * size).map((trade, index) => <tr key={`${page}-${index}`}>
            <td><span className={`side-badge ${trade.direction === "long" ? "positive" : trade.direction === "short" ? "negative" : ""}`}>{trade.direction ?? "Unknown side"}</span><small>{trade.symbol ?? "Unknown asset"}</small></td>
            <td>{date(trade.entry_time)}<small>{date(trade.exit_time)}</small></td>
            <td>{format(trade.entry_price, 8, 2)}<small>→ {format(trade.exit_price, 8, 2)}</small></td>
            <td>{format(trade.quantity, 8, 0)}</td>
            <td>{format(trade.stop_price, 8, 2)}<small>{format(trade.target_price, 8, 2)}</small></td>
            <td className={tone(trade.net_pnl)}>{signed(trade.net_pnl)}</td>
            <td>{format(trade.fees)}<small className={tone(trade.funding_pnl)}>{signed(trade.funding_pnl)}</small></td>
            <td>{trade.exit_reason?.replaceAll("_", " ") ?? "Not recorded"}</td>
          </tr>)}</tbody></table>
      </div><div className="toolbar ledger-pagination">
        <button disabled={page === 0} onClick={() => setPage(page - 1)}>Previous trades</button>
        <span>{page * size + 1}–{Math.min((page + 1) * size, trades.length)} of {trades.length}</span>
        <button disabled={(page + 1) * size >= trades.length} onClick={() => setPage(page + 1)}>Next trades</button>
      </div></>}
  </div>;
}

export function ResearchResults({ runs, detail, busy, inspect, close }: {
  runs: ResearchRun[]; detail: ResearchDetail | null; busy: boolean;
  inspect: (id: string) => void; close: () => void;
}) {
  const detailRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (detail) detailRef.current?.scrollIntoView({ block: "start" });
  }, [detail?.id]);
  const validation = detail?.dataset_metadata.kind === "walk_forward_search";
  const selected = detail?.metrics?.selected_holdout;
  const hasSelected = selected !== null && selected !== undefined && Object.keys(selected).length > 0;
  const metrics = validation
    ? (hasSelected ? selected : detail?.metrics?.baseline_holdout)
    : detail?.metrics;
  const scope = validation ? (hasSelected ? "selected_holdout" : "baseline_holdout") : "backtest";
  return <div className="research-results">
    {detail && <article ref={detailRef} className="research-detail" aria-label="Selected research result">
      <div className="research-heading">
        <div><span className="eyebrow">{scopeLabel(scope)}</span><h3>{metrics?.symbol ?? text(detail.dataset_metadata.symbol)} performance</h3>
          <small>{date(metrics?.start)} → {date(metrics?.end)} UTC</small></div>
        <div className="research-actions"><a className="action-link" href={csvUrl(detail.id)}>Download trades CSV</a>
          <button onClick={close}>Close result</button></div>
      </div>
      <p className="run-provenance">{detail.strategy_version ?? "Strategy not recorded"} · Run {detail.id}</p>
      {validation && <p className="research-notice">{scopeLabel(scope)} results only, not combined development performance. CSV exports this same holdout. Open technical details for all candidates and validation evidence.</p>}
      {metrics ? <>
        {metrics.halted && <p className="error" role="status">Simulation halted. Review risk diagnostics and technical details before interpreting results.</p>}
        <Performance metrics={metrics} />
        <EquityChart metrics={metrics} />
        {metrics.trades ? <TradeLedger key={detail.id} trades={metrics.trades} /> : <p>Trade ledger not recorded for this run.</p>}
        <details className="research-diagnostics"><summary>Strategy evaluation & risk diagnostics</summary>
          <p>{metrics.risk_rejections?.length ?? "Unrecorded"} rejected entry attempts. Counts below are evaluated flat-position bars, not unique signals or all candles.</p>
          {metrics.strategy_observations && <ul className="reason-list">
            {Object.entries(metrics.strategy_observations).sort((a, b) => b[1] - a[1]).map(([reason, count]) =>
              <li key={reason}><span>{reason}</span><strong>{count.toLocaleString()}</strong></li>)}
          </ul>}
          {!!metrics.risk_rejections?.length && <pre>{JSON.stringify(metrics.risk_rejections, null, 2)}</pre>}
        </details>
        {!!metrics.limitations?.length && <details className="research-diagnostics"><summary>Model assumptions & limitations</summary>
          <ul>{metrics.limitations.map(value => <li key={value}>{value}</li>)}</ul></details>}
      </> : <p className="research-empty">Performance metrics are not available for this run.</p>}
      <details className="research-diagnostics"><summary>Technical details & configuration snapshot</summary>
        <pre>{JSON.stringify(detail, null, 2)}</pre></details>
    </article>}
    <div className="research-heading"><div><h3>Saved research</h3><p>Immutable results · inspect a run to explore equity, trades and diagnostics.</p></div>
      <span className="research-pill">{runs.length} on this page</span></div>
    {runs.length === 0 ? <p className="research-empty">No research runs on this page. Start a public-data backtest above.</p> :
      <div className="research-run-grid">{runs.map(run => {
        const summary = run.summary ?? {};
        return <article key={run.id} className={`research-card${detail?.id === run.id ? " selected" : ""}`}>
          <div className="research-heading"><div><span className="eyebrow">{scopeLabel(summary.scope)}</span>
            <h4>{summary.symbol ?? text(run.dataset_metadata.symbol)}</h4></div>
            <span className="research-pill">{summary.halted ? "Halted" : run.status}</span></div>
          <small>{date(summary.start ?? run.dataset_metadata.start)} → {date(summary.end ?? run.dataset_metadata.end)} UTC</small>
          <Performance metrics={summary} compact />
          <div className="research-card-footer"><small>{run.strategy_version ?? "Strategy not recorded"}<br />Saved {date(run.created_at)} UTC</small>
            <div className="research-actions"><button disabled={busy} aria-label={`Inspect research ${run.id}`} aria-pressed={detail?.id === run.id}
              onClick={() => inspect(run.id)}>Inspect results</button>
              <a href={csvUrl(run.id)}>Trades CSV</a>
              {run.dataset_metadata.kind === "walk_forward_search" && <a href={`/api/research/${encodeURIComponent(run.id)}/validation.json`}>Validation JSON</a>}
            </div></div>
        </article>;
      })}</div>}
  </div>;
}
