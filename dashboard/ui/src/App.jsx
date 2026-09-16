import { useEffect, useMemo, useState } from 'react'
import { CartesianGrid, ComposedChart, Line, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import './App.css'

const percent = (value) => Number.isFinite(value) ? (value * 100).toFixed(2) + '%' : '—'
const optionPattern = /^RTM(\d+(?:\.\d+)?)([CP])$/
const optionColor = (symbol) => symbol.endsWith('C') ? '#2563eb' : '#e36b16'
const optionSort = (left, right) => {
  const leftMatch = left.match(optionPattern)
  const rightMatch = right.match(optionPattern)
  return Number(leftMatch?.[1]) - Number(rightMatch?.[1]) || leftMatch?.[2].localeCompare(rightMatch?.[2])
}

function TradeTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const row = payload[0].payload
  return <div className="tooltip"><strong>Tick {label}</strong><div>Analyst IV: {percent(row.analyst)}</div>{row.tradeDetail && <><hr /><strong>Trade decision</strong><div>{row.tradeDetail}</div></>}</div>
}

function ChartFrame({ title, description, rows, symbols, margin }) {
  const latest = rows.at(-1)
  return <article className="option-chart"><h3>{title}</h3><p>{description}</p><div className="chart chart-small"><ResponsiveContainer><ComposedChart data={rows} margin={{ top: 26, right: 24, bottom: 14, left: 4 }}><CartesianGrid stroke="#d9e3ee" strokeDasharray="3 3" /><XAxis dataKey="tick" type="number" domain={[0, 300]} ticks={[0, 100, 200, 300]} tick={{ fill: '#41516a', fontSize: 11 }} /><YAxis width={58} padding={{ top: 14, bottom: 14 }} tickFormatter={percent} tick={{ fill: '#41516a', fontSize: 11 }} domain={['auto', 'auto']} /><Tooltip content={<TradeTooltip />} /><ReferenceArea y1={latest?.analyst - margin} y2={latest?.analyst + margin} fill="#dcfce7" fillOpacity={0.4} /><Line isAnimationActive={false} connectNulls dataKey="analyst" stroke="#168043" strokeWidth={2} dot={false} name="Analyst IV" />{symbols.map((symbol) => <Line key={symbol} isAnimationActive={false} connectNulls dataKey={symbol} stroke={optionColor(symbol)} strokeWidth={2} dot={false} name={symbol} />)}{symbols.map((symbol) => <Line key={symbol + '-trade'} isAnimationActive={false} dataKey={(row) => row.trades?.[symbol] ? row[symbol] : null} stroke="none" dot={{ r: 6, fill: '#9333ea', stroke: '#ffffff', strokeWidth: 2 }} activeDot={false} name={symbol + ' trade'} />)}</ComposedChart></ResponsiveContainer></div></article>
}

function App() {
  const [records, setRecords] = useState([])
  const [error, setError] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(null)

  useEffect(() => {
    let live = true
    const poll = async () => {
      try {
        const response = await fetch('/api/decisions', { cache: 'no-store' })
        if (!response.ok) throw new Error('API returned ' + response.status)
        if (live) setRecords(await response.json())
        setError('')
      } catch (failure) {
        if (live) setError(failure.message)
      }
      if (live) window.setTimeout(poll, 1000)
    }
    poll()
    return () => { live = false }
  }, [])

  const { runRecords, runNumber } = useMemo(() => {
    let start = 0
    let number = 1
    records.forEach((record, index) => {
      if (index && record.tick < records[index - 1].tick) {
        start = index
        number += 1
      }
    })
    return { runRecords: records.slice(start), runNumber: number }
  }, [records])
  useEffect(() => { setSelectedIndex(null) }, [runNumber])
  const latest = runRecords[selectedIndex] || runRecords.at(-1)
  const optionSymbols = useMemo(() => [...new Set(runRecords.flatMap((record) => [...(record.options || []).map((option) => option.symbol), ...(record.desired_trades || []).map((trade) => trade.symbol)]).filter((symbol) => optionPattern.test(symbol)))].sort(optionSort), [runRecords])
  const strikes = useMemo(() => [...new Set(optionSymbols.map((symbol) => symbol.match(optionPattern)?.[1]))], [optionSymbols])
  const chartRows = useMemo(() => runRecords.map((record) => {
    const values = Object.fromEntries((record.options || []).map((option) => [option.symbol, option.market_iv]))
    const trades = (record.desired_trades || []).filter((trade) => optionPattern.test(trade.symbol) && trade.quantity)
    return { tick: record.tick, analyst: record.forecast?.sigma, ...values, trades: Object.fromEntries(trades.map((trade) => [trade.symbol, true])), tradeDetail: trades.map((trade) => (trade.quantity > 0 ? 'Buy ' : 'Sell ') + Math.abs(trade.quantity) + ' ' + trade.symbol).join(' + ') }
  }).filter((row) => Number.isFinite(row.analyst)), [runRecords])

  if (!latest) return <main><h1>Volatility decision monitor</h1><p>{error || 'Waiting for decision records…'}</p></main>
  const entry = latest.explanation?.entry_factors || {}
  const margin = entry.trade_iv_margin ?? 0.01
  const positions = [{ symbol: 'RTM', quantity: latest.rtm?.position ?? 0 }, ...(latest.options || []).filter((option) => option.position).map((option) => ({ symbol: option.symbol, quantity: option.position }))]

  return <main><header><div><h1>Volatility decision monitor</h1><p>Run {runNumber} · {runRecords.length} decisions in this run · {records.length} saved total · tick {latest.tick}</p></div><span className={error ? 'error' : 'healthy'}>{error || 'Live'}</span></header>
    <section className="metrics"><article><label>Option contracts tracked</label><strong className="blue">{optionSymbols.length}</strong></article><article><label>Analyst fair IV</label><strong className="green">{percent(latest.forecast?.sigma)}</strong></article><article><label>Decision</label><strong>{latest.reason}</strong></article></section>
    <section className="panel"><h2>Current confirmed positions</h2><p>Positions are read from the latest reconciled market snapshot, not from submitted-order intent.</p><div className="position-grid">{positions.some((position) => position.quantity) ? positions.filter((position) => position.quantity).map((position) => <article key={position.symbol}><label>{position.symbol}</label><strong className={position.quantity > 0 ? 'buy' : 'sell'}>{position.quantity > 0 ? '+' : ''}{position.quantity}</strong></article>) : <span className="muted">Flat: no RTM or option inventory.</span>}</div></section>
    <section className="panel"><h2>All option volatility and trade decisions</h2><p>Purple dots identify recorded option trade decisions by exact symbol. The current five strike pairs are displayed alongside an all-contract overview.</p><div className="charts-grid"><ChartFrame title="All contracts" description="Every listed option in this run." rows={chartRows} symbols={optionSymbols} margin={margin} />{strikes.map((strike) => { const symbols = ['RTM' + strike + 'C', 'RTM' + strike + 'P'].filter((symbol) => optionSymbols.includes(symbol)); return <ChartFrame key={strike} title={'RTM ' + strike + ' options'} description="Blue call · orange put · green analyst IV." rows={chartRows} symbols={symbols} margin={margin} /> })}</div></section>
    <section className="panel"><h2>Decision history</h2><p>This view resets automatically when the competition tick resets. Earlier runs remain in the append-only log.</p><div className="history"><table><thead><tr><th>Tick</th><th>Option trades</th><th>Analyst IV</th><th>Edge</th><th>Action</th><th>Delta</th></tr></thead><tbody>{runRecords.map((record, index) => { const trades = (record.desired_trades || []).filter((trade) => optionPattern.test(trade.symbol)); return <tr key={record.tick + '-' + index} className={index === (selectedIndex ?? runRecords.length - 1) ? 'selected' : ''} onClick={() => setSelectedIndex(index)}><td>{record.tick}</td><td className="purple">{trades.map((trade) => (trade.quantity > 0 ? 'Buy ' : 'Sell ') + Math.abs(trade.quantity) + ' ' + trade.symbol).join(', ') || '—'}</td><td className="green">{percent(record.forecast?.sigma)}</td><td>{record.straddle?.edge ? '$' + record.straddle.edge.toFixed(2) : '—'}</td><td>{record.reason}</td><td>{record.portfolio?.delta?.toFixed(0) ?? '—'}</td></tr>})}</tbody></table></div></section>
    <section className="panel"><h2>Why this trade or wait</h2><pre>{JSON.stringify({ forecast: latest.explanation?.forecast_factors, entry, risk: latest.explanation?.risk_factors, costs: latest.explanation?.cost_assumptions }, null, 2)}</pre></section>
  </main>
}

export default App
