// Local UI (ADR-0016): four tabs over the read API, live dataset updates and the alert stream.
// Data reaches the page only as JSON; strings are escaped (w2utils.encodeTags) before w2ui
// renders them, and everything else is written with textContent.
import { w2grid, w2tabs, w2utils } from './vendor/w2ui-2.0.0/w2ui-2.0.es6.min.js'
import { lineChart } from './chart.js'

const STALE_DAYS = 4
const $ = sel => document.querySelector(sel)
const state = {
    tab: 'curves', csrf: null, streams: {},
    alerts: new Map(), alertEpoch: null, alertCursor: null, alertRequest: 0, // event id -> alert
    historyRequest: 0,
}

// Counts processed responses per loader, so browser tests can wait for one deterministically.
function processed(name) {
    const data = document.body.dataset
    data[name] = String(Number(data[name] || 0) + 1)
}

async function parse(response) {
    if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`)
    }
    return response.status === 204 ? null : response.json()
}

async function api(path, options = {}) {
    return parse(await fetch(path, options))
}

// The CSRF token belongs to the server process, so after a server restart the page's token is
// stale. A change refused for its token fetches the current one and is sent once more.
async function mutate(path, options) {
    const send = () => fetch(path, { ...options, headers: { ...options.headers, 'x-csrf-token': state.csrf } })
    let response = await send()
    if (response.status === 403) {
        const detail = (await response.clone().json().catch(() => ({}))).detail
        if (typeof detail === 'string' && detail.includes('CSRF')) {
            state.csrf = (await api('/api/csrf')).token
            response = await send()
        }
    }
    return parse(response)
}

const safe = row => Object.fromEntries(
    Object.entries(row).map(([k, v]) => [k, typeof v === 'string' ? w2utils.encodeTags(v) : v]))
const records = (rows, key) => rows.map((r, i) => ({ recid: key ? r[key] : i + 1, ...safe(r) }))

function grid(name, box, columns) {
    return new w2grid({
        name, box, columns: columns.map(([field, text, size]) => ({ field, text, size: size || '120px', sortable: true })),
        show: { footer: true }, records: [],
    })
}

const grids = {
    curves: grid('curves', '#grid-curves', [
        ['curve_id', 'Curve'], ['position', 'Position', '80px'], ['as_of', 'As of', '110px'],
        ['price', 'Price', '100px'], ['status', 'Status', '80px'], ['data_age_days', 'Age (days)', '90px'],
        ['estimate_type', 'Estimate', '110px'], ['gap_reason', 'Gap reason', '160px']]),
    history: grid('history', '#grid-history', [
        ['as_of', 'As of', '110px'], ['price', 'Price', '100px'], ['status', 'Status', '80px'],
        ['gap_reason', 'Gap reason', '200px'], ['estimate_type', 'Estimate', '110px']]),
    rules: grid('rules', '#grid-rules', [
        ['curve_id', 'Curve'], ['position', 'Position', '80px'], ['threshold', 'Above', '100px'],
        ['armed', 'Armed', '70px'], ['last_value', 'Last value', '100px'], ['last_as_of', 'Last as of', '110px'],
        ['created_at', 'Created', '200px']]),
    alerts: grid('alerts', '#grid-alerts', [
        ['fired_at', 'Fired', '200px'], ['curve_id', 'Curve'], ['position', 'Position', '80px'],
        ['price', 'Price', '100px'], ['previous_price', 'Previous', '100px'], ['threshold', 'Above', '100px'],
        ['as_of', 'As of', '110px'], ['dataset_version', 'Version', '80px']]),
    versions: grid('versions', '#grid-versions', [
        ['dataset_version', 'Version', '80px'], ['source', 'Source', '100px'], ['created_at', 'Created', '200px'],
        ['observations', 'Observations', '110px'], ['curve_points', 'Curve points', '110px'],
        ['price_changes', 'Price changes', '110px']]),
    attempts: grid('attempts', '#grid-attempts', [
        ['status', 'Status', '100px'], ['finished_at', 'Finished', '200px'], ['error', 'Error', '300px'],
        ['attempt_id', 'Attempt', '280px']]),
    dead: grid('dead', '#grid-dead', [
        ['dataset_version', 'Version', '80px'], ['event_type', 'Type', '140px'], ['attempts', 'Attempts', '80px'],
        ['last_error', 'Last error', '300px']]),
}

function setRecords(name, rows, key) {
    grids[name].records = records(rows, key)
    grids[name].total = rows.length
    grids[name].refresh()
}

function toast(text) {
    const box = $('[data-testid=toast]')
    box.textContent = text
    box.hidden = false
    clearTimeout(toast.timer)
    toast.timer = setTimeout(() => { box.hidden = true }, 6000)
}

function header(envelope, curves) {
    $('[data-testid=dataset-version]').textContent = envelope.dataset_version
        ? `dataset v${envelope.dataset_version}` : 'no data yet'
    $('[data-testid=synthetic-badge]').hidden = !envelope.synthetic
    const stale = $('[data-testid=stale-banner]')
    const freshest = curves?.length ? curves.reduce((a, c) => (c.data_age_days < a.data_age_days ? c : a)) : null
    stale.hidden = !freshest || freshest.data_age_days <= STALE_DAYS
    if (freshest) stale.textContent = `Data is ${freshest.data_age_days} days old (latest as of ${freshest.as_of}).`
}

async function loadCurves() {
    try {
        const body = await api('/api/curves')
        header(body, body.curves)
        $('[data-testid=curves-label]').textContent = `${body.label} · ${body.disclaimer}`
        setRecords('curves', body.curves.flatMap(c => c.points.map(p => ({ curve_id: c.curve_id, ...p }))))
    } catch (e) {
        header({}, null)
        setRecords('curves', [])
        $('[data-testid=curves-label]').textContent = e.message
    }
}

function historyParams() {
    const form = new FormData($('[data-testid=history-filters]'))
    const params = new URLSearchParams()
    for (const [k, v] of form) if (v) params.set(k, v)
    return params
}

// Only the answer to the latest request is drawn: a slower answer for an earlier selection is
// ignored. Heading and export link come from the answer itself, so they always match the chart.
async function loadHistory() {
    const request = ++state.historyRequest
    const chart = $('#chart')
    chart.style.opacity = '0.5' // refetch keeps the frame
    try {
        const body = await api(`/api/history/curves?${historyParams()}`)
        if (request !== state.historyRequest) return
        const title = `${body.curve_id} ${body.position}`
        const shown = new URLSearchParams()
        for (const k of ['curve_id', 'position', 'start', 'end']) if (body[k]) shown.set(k, body[k])
        $('[data-testid=history-title]').textContent = title
        $('[data-testid=export-history]').href = `/api/export/history.csv?${shown}`
        lineChart(chart, body.points, { label: title })
        setRecords('history', body.points)
    } catch (e) {
        if (request !== state.historyRequest) return
        $('[data-testid=history-title]').textContent = ''
        $('[data-testid=export-history]').removeAttribute('href')
        chart.replaceChildren(document.createTextNode(e.message))
        setRecords('history', [])
    } finally {
        if (request === state.historyRequest) chart.style.opacity = '1'
        processed('historyLoads')
    }
}

async function loadRules() {
    setRecords('rules', (await api('/api/alerts/rules')).rules, 'rule_id')
}

function renderAlerts() {
    const rows = [...state.alerts].map(([id, a]) => ({ recid: id, ...safe(a) }))
    grids.alerts.records = rows.sort((a, b) => b.seq - a.seq) // newest first
    grids.alerts.total = rows.length
    grids.alerts.refresh()
}

function addAlert(alert, id) {
    if (state.alerts.has(id)) return false // de-duplicated by event id
    state.alerts.set(id, alert)
    renderAlerts()
    return true
}

// A snapshot is merged into what the page already holds, never replaces it: a snapshot taken
// before an alert fired can answer after the stream delivered that alert. A snapshot answering
// after a newer one was requested is ignored. Only a new epoch (a restored log) drops alerts,
// and then only those of other epochs.
async function loadAlerts() {
    const request = ++state.alertRequest
    try {
        const body = await api('/api/alerts')
        if (request !== state.alertRequest) return
        const epoch = body.cursor.split('-')[0]
        if (epoch !== state.alertEpoch) {
            for (const id of [...state.alerts.keys()]) if (!id.startsWith(`${epoch}-`)) state.alerts.delete(id)
            state.alertEpoch = epoch
        }
        for (const a of body.alerts) state.alerts.set(`${epoch}-${a.seq}`, a)
        state.alertCursor ??= body.cursor
        renderAlerts()
    } finally {
        processed('alertSnapshots')
    }
}

async function loadHealth() {
    const body = await api('/api/health')
    $('[data-testid=outbox-counts]').textContent =
        `Outbox: ${Object.entries(body.outbox.counts).map(([k, v]) => `${v} ${k}`).join(', ') || 'empty'}`
    setRecords('versions', body.versions)
    setRecords('attempts', body.attempts.failed_or_quarantined)
    setRecords('dead', body.outbox.dead)
}

const loaders = { curves: loadCurves, history: loadHistory, alerts: () => Promise.all([loadRules(), loadAlerts()]), health: loadHealth }

async function refresh() {
    await loadCurves()
    if (state.tab !== 'curves') await loaders[state.tab]()
}

function show(tab) {
    state.tab = tab
    for (const name of Object.keys(loaders)) $(`#panel-${name}`).hidden = name !== tab
    for (const g of Object.values(grids)) g.resize()
    loaders[tab]()
}

function liveStatus() {
    const open = Object.values(state.streams).every(s => s.readyState === EventSource.OPEN)
    const status = $('[data-testid=live-status]')
    status.textContent = open ? 'live' : 'reconnecting'
    status.classList.toggle('on', open)
}

function stream(name, url, handlers) {
    const source = new EventSource(url)
    state.streams[name] = source
    source.onopen = liveStatus
    source.onerror = liveStatus // the browser reconnects itself, sending Last-Event-ID
    for (const [event, handler] of Object.entries(handlers)) source.addEventListener(event, handler)
}

async function fillSelects() {
    const catalog = await api('/api/catalog')
    for (const form of ['history-filters', 'rule-form']) {
        for (const [name, values] of [['curve_id', catalog.curves], ['position', catalog.positions]]) {
            const select = $(`[data-testid=${form}] select[name=${name}]`)
            select.replaceChildren(...values.map(v => new Option(v, v)))
        }
    }
}

async function main() {
    new w2tabs({
        box: '#tabs', name: 'tabs', active: 'curves',
        tabs: [{ id: 'curves', text: 'Curves' }, { id: 'history', text: 'History' },
               { id: 'alerts', text: 'Alerts' }, { id: 'health', text: 'Health' }],
        onClick(event) { show(event.target) },
    })
    await fillSelects()
    state.csrf = (await api('/api/csrf')).token
    $('[data-testid=history-filters]').addEventListener('submit', e => { e.preventDefault(); loadHistory() })
    $('[data-testid=rule-form]').addEventListener('submit', async e => {
        e.preventDefault()
        const form = new FormData(e.target)
        $('[data-testid=rule-error]').textContent = ''
        try {
            await mutate('/api/alerts/rules', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify(Object.fromEntries(form)),
            })
            e.target.threshold.value = ''
            await loadRules()
        } catch (err) {
            $('[data-testid=rule-error]').textContent = err.message
        }
    })
    $('[data-testid=delete-rule]').addEventListener('click', async () => {
        const [id] = grids.rules.getSelection()
        if (!id) return
        $('[data-testid=rule-error]').textContent = ''
        try {
            await mutate(`/api/alerts/rules/${encodeURIComponent(id)}`, { method: 'DELETE' })
            await loadRules()
        } catch (err) {
            $('[data-testid=rule-error]').textContent = err.message
        }
    })
    await loadCurves()
    await loadAlerts()
    stream('dataset', '/api/events', { dataset_updated: () => refresh() })
    stream('alerts', `/api/alerts/events?after=${encodeURIComponent(state.alertCursor)}`, {
        alert_fired: e => {
            const alert = JSON.parse(e.data)
            if (addAlert(alert, e.lastEventId)) {
                toast(`Alert: ${alert.curve_id} ${alert.position} at ${alert.price} crossed ${alert.threshold}`)
                if (state.tab === 'alerts') loadRules()
            }
        },
        alerts_reset: () => loadAlerts(),
    })
    document.body.dataset.ready = 'true' // for tests: initial load done
}

main().catch(e => toast(`Failed to load: ${e.message}`))
