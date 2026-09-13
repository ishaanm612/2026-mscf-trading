import { useEffect, useMemo, useState } from 'react'
import { CartesianGrid, Line, LineChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import './App.css'

const percent = (value) => Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—'

function EntryDot({ cx, cy, payload }) {
  return payload.entry ? <circle cx={cx} cy={cy} r={6} fill="#9333ea" stroke="#ffffff" strokeWidth={2} /> : null
}

function EntryTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const row = payload[0].payload
  return <div className="tooltip"><strong>Tick {label}</strong><div>Actual ATM IV: {percent(row.actual)}</div><div>Analyst IV: {percent(row.analyst)}</div>{row.entry && <><hr /><strong>Entry signal</strong><div>{row.entryDetail}</div><div>Estimated edge: ${row.entryEdge.toFixed(2)} per straddle</div></>}</div>
}

function atmIv(record) {
  const strike = record.straddle?.strike
  if (strike === undefined) return null
  const values = record.options.filter((option) => option.symbol === `RTM${strike}C` || option.symbol === `RTM${strike}P`).map((option) => option.market_iv).filter(Number.isFinite)
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null
}

function App() {
  const [records, setRecords] = useState([])
  const [error, setError] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(null)
  useEffect(() => { let live = true; const poll = async () => { try { const response = await fetch('/api/decisions', { cache: 'no-store' }); if (!response.ok) throw new Error(`API returned ${response.status}`); if (live) setRecords(await response.json()); setError('') } catch (failure) { if (live) setError(failure.message) } if (live) window.setTimeout(poll, 1000) }; poll(); return () => { live = false } }, [])
  const { runRecords, runNumber } = useMemo(() => { let start = 0; let number = 1; records.forEach((record, index) => { if (index && record.tick < records[index - 1].tick) { start = index; number += 1 } }); return { runRecords: records.slice(start), runNumber: number } }, [records])
  useEffect(() => { setSelectedIndex(null) }, [runNumber])
  const latest = runRecords[selectedIndex] || runRecords.at(-1)
  const rows = useMemo(() => runRecords.map((record) => { const analyst = record.forecast?.sigma; const margin = record.explanation?.entry_factors?.trade_iv_margin ?? 0.01; const trades = record.desired_trades || []; const entry = trades.some((trade) => trade.reason === 'ATM volatility straddle'); return { tick: record.tick, actual: atmIv(record), analyst, lower: analyst - margin, upper: analyst + margin, entry, entryEdge: record.straddle?.edge ?? 0, entryDetail: trades.map((trade) => `${trade.quantity > 0 ? 'Buy' : 'Sell'} ${Math.abs(trade.quantity)} ${trade.symbol}`).join(' + ') } }).filter((row) => Number.isFinite(row.actual) && Number.isFinite(row.analyst)), [runRecords])
  if (!latest) return <main><h1>Volatility decision monitor</h1><p>{error || 'Waiting for decision records…'}</p></main>
  const factors = latest.explanation || {}; const entry = factors.entry_factors || {}; const margin = entry.trade_iv_margin ?? 0.01
  return <main><header><div><h1>Volatility decision monitor</h1><p>Run {runNumber} · {runRecords.length} decisions in this run · {records.length} saved total · tick {latest.tick}</p></div><span className={error ? 'error' : 'healthy'}>{error || 'Live'}</span></header>
    <section className="metrics"><article><label>Actual ATM market IV</label><strong className="blue">{percent(atmIv(latest))}</strong></article><article><label>Analyst fair IV</label><strong className="green">{percent(latest.forecast?.sigma)}</strong></article><article><label>Decision</label><strong>{latest.reason}</strong></article></section>
    <section className="panel"><h2>Actual versus analyst volatility</h2><p>Blue is current ATM market implied volatility. Green is analyst fair IV. The green shaded band is the margin needed before entry. Purple points sit directly on the blue line at straddle entry signals; hover one for its details.</p><div className="chart"><ResponsiveContainer><LineChart data={rows}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="tick" type="number" domain={['dataMin', 'dataMax']} /><YAxis tickFormatter={percent} domain={['auto', 'auto']} /><Tooltip content={<EntryTooltip />} /><ReferenceArea y1={latest.forecast.sigma - margin} y2={latest.forecast.sigma + margin} fill="#dcfce7" fillOpacity={0.5} /><Line dataKey="actual" stroke="#2563eb" strokeWidth={3} dot={<EntryDot />} activeDot={{ r: 7 }} name="Actual ATM IV" /><Line dataKey="analyst" stroke="#16a34a" strokeWidth={3} dot={false} name="Analyst IV" /><Line dataKey="lower" stroke="#16a34a" strokeDasharray="6 4" dot={false} name="Entry lower bound" /><Line dataKey="upper" stroke="#16a34a" strokeDasharray="6 4" dot={false} name="Entry upper bound" /></LineChart></ResponsiveContainer></div></section>
    <section className="panel"><h2>Decision history</h2><p>This view resets automatically when the competition tick resets. Earlier runs remain in the append-only log.</p><div className="history"><table><thead><tr><th>Tick</th><th>Market IV</th><th>Analyst IV</th><th>Edge</th><th>Action</th><th>Delta</th></tr></thead><tbody>{runRecords.map((record, index) => <tr key={`${record.tick}-${index}`} className={index === (selectedIndex ?? runRecords.length - 1) ? 'selected' : ''} onClick={() => setSelectedIndex(index)}><td>{record.tick}</td><td className="blue">{percent(atmIv(record))}</td><td className="green">{percent(record.forecast?.sigma)}</td><td>{record.straddle?.edge ? `$${record.straddle.edge.toFixed(2)}` : '—'}</td><td>{record.desired_trades?.map((trade) => `${trade.quantity > 0 ? 'Buy' : 'Sell'} ${Math.abs(trade.quantity)} ${trade.symbol}`).join(', ') || record.reason}</td><td>{record.portfolio?.delta?.toFixed(0) ?? '—'}</td></tr>)}</tbody></table></div></section>
    <section className="panel"><h2>Why this trade or wait</h2><pre>{JSON.stringify({ forecast: factors.forecast_factors, entry, risk: factors.risk_factors, costs: factors.cost_assumptions }, null, 2)}</pre></section>
  </main>
}

export default App
