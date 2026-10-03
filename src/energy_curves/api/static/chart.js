// One-series line chart in plain SVG (ADR-0016). Follows the dataviz mark specs: 2px line with
// round joins, gaps break the line, an end dot with a 2px surface ring, hairline solid grid,
// recessive axes, a crosshair that snaps to the nearest date, a tooltip that leads with the
// value, and the same readout on keyboard focus (arrow keys). Text is set with textContent only.
// The table below the chart is its text alternative.

const SVG = 'http://www.w3.org/2000/svg'
const INK = { primary: '#0b0b0b', secondary: '#52514e', muted: '#898781', grid: '#e1e0d9' }
const SERIES = '#2a78d6' // validated against the #fcfcfb surface (dataviz validator, light mode)
const SURFACE = '#fcfcfb'
const M = { top: 12, right: 16, bottom: 28, left: 56 }

function el(name, attrs = {}, parent = null) {
    const node = document.createElementNS(SVG, name)
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v))
    if (parent) parent.appendChild(node)
    return node
}

function niceTicks(min, max, count = 4) {
    const span = max - min || Math.abs(max) || 1
    const raw = span / count
    const step = [1, 2, 2.5, 5, 10].map(m => m * 10 ** Math.floor(Math.log10(raw))).find(s => s >= raw)
    const ticks = [] // from below the minimum to above the maximum, so every value is inside
    const last = Math.ceil(max / step) * step
    for (let t = Math.floor(min / step) * step; t <= last + step / 1e6; t += step) ticks.push(+t.toFixed(10))
    return ticks
}

export function lineChart(container, points, { label }) {
    container.replaceChildren()
    const width = Math.max(container.clientWidth || 600, 280)
    const height = 220
    const svg = el('svg', {
        width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', tabindex: 0,
        'aria-label': `${label}: line chart of ${points.length} points; the table below lists every value`,
        'data-testid': 'history-chart',
    }, container)
    svg.style.background = SURFACE
    const tooltip = document.createElement('div')
    tooltip.className = 'chart-tooltip'
    tooltip.hidden = true
    container.appendChild(tooltip)

    const values = points.map(p => (p.price === null ? null : Number(p.price)))
    const known = values.filter(v => v !== null)
    if (known.length === 0) {
        const t = el('text', { x: width / 2, y: height / 2, 'text-anchor': 'middle', fill: INK.muted }, svg)
        t.textContent = 'No prices in this range'
        return
    }
    const times = points.map(p => Date.parse(p.as_of))
    const [t0, t1] = [times[0], times[times.length - 1]]
    const ticks = niceTicks(Math.min(...known), Math.max(...known))
    const [y0, y1] = [ticks[0], ticks[ticks.length - 1]]
    const x = t => M.left + ((t - t0) / (t1 - t0 || 1)) * (width - M.left - M.right)
    const y = v => height - M.bottom - ((v - y0) / (y1 - y0 || 1)) * (height - M.top - M.bottom)

    for (const tick of ticks) { // hairline grid + y labels in muted ink
        el('line', { x1: M.left, x2: width - M.right, y1: y(tick), y2: y(tick), stroke: INK.grid, 'stroke-width': 1 }, svg)
        const t = el('text', { x: M.left - 6, y: y(tick) + 4, 'text-anchor': 'end', fill: INK.muted, 'font-size': 11 }, svg)
        t.textContent = tick.toFixed(2)
    }
    for (const i of [0, Math.floor((points.length - 1) / 2), points.length - 1]) {
        const t = el('text', { x: x(times[i]), y: height - 8, 'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle', fill: INK.muted, 'font-size': 11 }, svg)
        t.textContent = points[i].as_of
    }

    let d = '' // gaps (null prices) break the line
    values.forEach((v, i) => {
        if (v === null) return
        d += `${i === 0 || values[i - 1] === null ? 'M' : 'L'}${x(times[i]).toFixed(1)},${y(v).toFixed(1)}`
    })
    el('path', { d, fill: 'none', stroke: SERIES, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'data-testid': 'history-line' }, svg)
    const last = values.map((v, i) => [v, i]).filter(([v]) => v !== null).pop()
    el('circle', { cx: x(times[last[1]]), cy: y(last[0]), r: 4, fill: SERIES, stroke: SURFACE, 'stroke-width': 2 }, svg)

    const cross = el('line', { y1: M.top, y2: height - M.bottom, stroke: INK.muted, 'stroke-width': 1, visibility: 'hidden' }, svg)
    const dot = el('circle', { r: 4, fill: SERIES, stroke: SURFACE, 'stroke-width': 2, visibility: 'hidden' }, svg)
    let focus = points.length - 1

    function show(i) {
        focus = i
        const px = x(times[i])
        cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('visibility', 'visible')
        if (values[i] !== null) {
            dot.setAttribute('cx', px); dot.setAttribute('cy', y(values[i])); dot.setAttribute('visibility', 'visible')
        } else dot.setAttribute('visibility', 'hidden')
        const strong = document.createElement('strong')
        strong.textContent = points[i].price ?? 'gap'
        const when = document.createElement('span')
        when.textContent = ` ${points[i].as_of}`
        tooltip.replaceChildren(strong, when)
        tooltip.hidden = false
        tooltip.style.left = `${Math.min(px + 8, width - 140)}px`
        tooltip.style.top = `${M.top}px`
    }
    function hide() {
        cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); tooltip.hidden = true
    }
    svg.addEventListener('pointermove', e => {
        const px = e.clientX - svg.getBoundingClientRect().left
        let best = 0
        times.forEach((t, i) => { if (Math.abs(x(t) - px) < Math.abs(x(times[best]) - px)) best = i })
        show(best)
    })
    svg.addEventListener('pointerleave', hide)
    svg.addEventListener('focus', () => show(focus))
    svg.addEventListener('blur', hide)
    svg.addEventListener('keydown', e => {
        if (e.key === 'ArrowLeft') { show(Math.max(0, focus - 1)); e.preventDefault() }
        if (e.key === 'ArrowRight') { show(Math.min(points.length - 1, focus + 1)); e.preventDefault() }
    })
}
