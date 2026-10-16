import { useEffect, useState } from "react";

type Contract = { symbol: string; base_asset: string; quote_asset: "USDC" | "USDT"; margin_asset: string };
type Catalog = {
  environment: "production" | "testnet";
  quote_asset: "USDC" | "USDT";
  unverified_simulation_max_leverage: number;
  exchange_leverage_verified: boolean;
  items: Contract[];
};
type Props = { quote: "USDC" | "USDT"; selected: string[]; onAdd: (symbol: string) => void };

export function assetLabel(symbol: string, quote: "USDC" | "USDT"): string {
  return symbol.endsWith(quote) ? `${symbol.slice(0, -quote.length)} / ${quote}` : symbol;
}

export function AssetPicker({ quote, selected, onAdd }: Props) {
  const [environment, setEnvironment] = useState<"production" | "testnet">("production");
  const [query, setQuery] = useState("");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setCatalog(null);
    void (async () => {
      try {
        const response = await fetch(`/api/market/contracts?quote_asset=${quote}&environment=${environment}`, {
          credentials: "same-origin", signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Contract list unavailable (HTTP ${response.status}). Retry or sign in again.`);
        const result: Catalog = await response.json();
        if (!controller.signal.aborted) setCatalog(result);
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "Contract list request failed.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [quote, environment, revision]);

  const current = catalog?.quote_asset === quote && catalog.environment === environment ? catalog : null;
  const search = query.trim().toUpperCase();
  const matches = current?.items.filter(item => item.symbol.includes(search) || item.base_asset.includes(search)) ?? [];

  return <div className="asset-picker">
    <h3>Find assets</h3>
    <p>Choose trading perpetual contracts quoted and margined in {quote}. New assets start enabled at 1×; nothing is saved until you save risk settings.</p>
    <label>Contract directory<select value={environment} onChange={event => {
      setEnvironment(event.target.value === "testnet" ? "testnet" : "production");
    }}>
      <option value="production">Binance public market data</option>
      <option value="testnet">Binance testnet market data</option>
    </select></label>
    <p>This selects a read-only directory, not a trading environment. Testnet availability may differ.</p>
    <label>Search assets<input type="search" placeholder="Search BTC, ETH, SOL…" value={query} onChange={event => setQuery(event.target.value)} /></label>
    <button type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>Refresh contract list</button>
    {loading && <p role="status">Loading {quote} contracts…</p>}
    {error && <p role="alert" className="error">{error} Existing selections are unchanged.</p>}
    {!loading && !error && current && <>
      <p role="status">{matches.length} matching contracts · {selected.length} selected{matches.length > 60 ? " · Showing first 60; refine your search." : ""}</p>
      {matches.length === 0 && <p>No matching {quote} contracts in this directory. Try another search or directory; no USDT/USDC fallback is applied.</p>}
      <ul className="contract-list" aria-label="Available assets">
        {matches.slice(0, 60).map(item => {
          const added = selected.includes(item.symbol);
          return <li key={item.symbol}>
            <div><strong>{item.base_asset} / {item.quote_asset}</strong><small>{item.symbol} · Perpetual</small></div>
            <button type="button" disabled={added} aria-label={`${added ? "Selected" : "Add"} ${item.base_asset} / ${item.quote_asset}`} onClick={() => onAdd(item.symbol)}>{added ? "Selected" : "Add"}</button>
          </li>;
        })}
      </ul>
    </>}
  </div>;
}
