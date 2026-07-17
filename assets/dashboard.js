
// ---- Helpers
function fmtMoney(v, sym) {
  sym = sym || BASE_SYMBOL;
  return sym + (v >= 1000 ? v.toLocaleString('en-GB', {maximumFractionDigits: 2}) : v.toFixed(2));
}
function fmtPct(v, sign) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const s = (sign && v >= 0) ? '+' : '';
  return s + v.toFixed(2) + '%';
}
function fmtDate(iso) {
  const [y, m, d] = iso.split('-');
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return parseInt(d) + ' ' + months[parseInt(m)-1] + ' ' + y.slice(2);
}
// Compact form for the x-axis ticks (no day-of-month): "Oct '24". The axis spans
// months, so the day is noise and crowds/overlaps on narrow (mobile) charts.
function fmtAxisDate(iso) {
  const [y, m] = iso.split('-');
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return months[parseInt(m)-1] + " '" + y.slice(2);
}

// ---- Hero chart (basket + SPY)
let showNasdaq = (function() { try { return localStorage.getItem('heroNasdaq') === '1'; } catch (e) { return false; } })();
let showIndustries = (function() { try { return localStorage.getItem('heroIndustries') === '1'; } catch (e) { return false; } })();
let heroRange = 'all';   // v3.4 #3: 'all' | '3m' | '1m' (transient, not persisted)
function rebaseWindow(vals) {
  if (!vals.length) return [];
  const base = 1 + vals[0] / 100;
  if (base === 0) return vals.map(() => 0);
  return vals.map(v => ((1 + v / 100) / base - 1) * 100);
}

// v3.6 what-if: selection state (persisted) + equal-weight math mirroring the
// Python builder. Lines render only in the 3M/1M window (short mode).
let whatIfSel = [];
try { whatIfSel = JSON.parse(localStorage.getItem('whatIfSelection') || '[]'); } catch (e) {}
if (!Array.isArray(whatIfSel)) whatIfSel = [];
let whatIfOn = false, whatIfBlendedOn = false;
try {
  whatIfOn = localStorage.getItem('whatIfOn') === '1';
  whatIfBlendedOn = localStorage.getItem('whatIfBlendedOn') === '1';
} catch (e) {}
function whatIfData() { return (typeof WHATIF !== 'undefined' && WHATIF) ? WHATIF : {dates: [], n_open: 0, names: {}}; }
function whatIfMembers() {
  // selected tickers whose series fully covers the payload window
  const wi = whatIfData(), n = (wi.dates || []).length;
  return whatIfSel.filter(t => {
    const d = wi.names && wi.names[t];
    return d && d.cum && d.cum.length === n && n > 0;
  });
}
function compoundToCum(daily) {
  let g = 1; const out = [];
  for (const r of daily) { g *= (1 + r); out.push((g - 1) * 100); }
  if (out.length) { const b = 1 + out[0] / 100; return out.map(v => ((1 + v / 100) / b - 1) * 100); }
  return out;
}
function computeWhatIfSeries(i0) {
  const wi = whatIfData(), members = whatIfMembers();
  if (!members.length) return null;
  const n = wi.dates.length;
  const daily = [];
  for (let i = i0; i < n; i++) {
    let g = 0;
    for (const t of members) {
      const c = wi.names[t].cum;
      const gPrev = 1 + (i > i0 ? c[i - 1] : c[i0]) / 100;
      g += (1 + c[i] / 100) / gPrev - 1;          // per-member daily return
    }
    daily.push(g / members.length);                // equal-weight mean
  }
  return compoundToCum(daily);
}
function computeBlendedSeries(i0, basketWinCum) {
  // (N*r_basket + k*r_custom)/(N+k) per day, then compound + rebase. Daily
  // returns are invariant to rebasing, so basketWinCum may be rebased or raw.
  const custom = computeWhatIfSeries(i0);
  if (!custom || !basketWinCum || !basketWinCum.length) return null;
  const N = whatIfData().n_open || 1, k = whatIfMembers().length;
  const daily = [];
  for (let i = 0; i < custom.length; i++) {
    const rb = i === 0 ? 0 : (1 + basketWinCum[i] / 100) / (1 + basketWinCum[i - 1] / 100) - 1;
    const rc = i === 0 ? 0 : (1 + custom[i] / 100) / (1 + custom[i - 1] / 100) - 1;
    daily.push((N * rb + k * rc) / (N + k));
  }
  return compoundToCum(daily);
}
function renderHeroChart() {
  const svg = document.getElementById('hero-chart');
  const wrap = svg.parentElement;
  const tip = document.getElementById('hero-tip');
  const W = Math.max(wrap.clientWidth, 320);
  const H = Math.max(wrap.clientHeight, 200);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);

  const isAll = heroRange === 'all';
  let b = PORTFOLIO.basket;
  let s = PORTFOLIO.spy;
  let fx = PORTFOLIO.fx || {dates:[], values:[]};
  const nzRaw = PORTFOLIO.nasdaq || {dates:[], values:[]};
  let nz = showNasdaq ? nzRaw : {dates:[], values:[]};
  const indRaw = PORTFOLIO.industries || [];
  let ind = showIndustries ? indRaw : [];
  // v3.4 #3: short-term window — rebase basket+SPY to the window start and drop
  // the since-inception extras (FX/Nasdaq/industries self-skip on empty arrays).
  // v3.6: what-if + blended lines are computed here too (short mode only).
  let wiSeries = null, blSeries = null;
  if (!isAll) {
    const days = heroRange === '1m' ? 30 : 90;
    const st = PORTFOLIO.short || {dates:[], basket:[], spy:[]};
    let i0 = 0;
    if (st.dates.length) {
      const cut = Date.parse(st.dates[st.dates.length - 1]) - days * 86400000;
      while (i0 < st.dates.length && Date.parse(st.dates[i0]) < cut) i0++;
      if (i0 >= st.dates.length) i0 = 0;
    }
    const wd = st.dates.slice(i0);
    b = {dates: wd, values: rebaseWindow(st.basket.slice(i0))};
    s = {dates: wd, values: rebaseWindow(st.spy.slice(i0))};
    fx = {dates:[], values:[]}; nz = {dates:[], values:[]}; ind = [];
    // v3.6: what-if custom basket + blended line, aligned to the same window.
    // Requires WHATIF.dates === PORTFOLIO.short.dates (both from the short slice).
    if ((whatIfOn || whatIfBlendedOn) && whatIfData().dates.length === st.dates.length) {
      const wi = computeWhatIfSeries(i0);
      if (wi && whatIfOn) wiSeries = {dates: wd, values: wi};
      if (wi && whatIfBlendedOn) {
        const bl = computeBlendedSeries(i0, b.values);
        if (bl) blSeries = {dates: wd, values: bl};
      }
    }
  }
  // Only OWNED industry lines (real trajectories) drive the y-scale. Non-owned
  // 12m endpoints are clamped into the plot area instead, so a large or glitchy
  // outlook return can never flatten the chart.
  const indVals = [];
  ind.forEach(e => { if (e.series) indVals.push(...e.series.values); });
  if (!b.values.length) { svg.innerHTML = '<text x="50%" y="50%" fill="#6b7185" font-family="Geist Mono" font-size="12" text-anchor="middle">No basket data</text>'; return; }

  // Combined min/max across both series. v3.6: the what-if + blended lines are
  // real trajectories the user chose to see, so (unlike non-owned industry
  // markers) they DO drive the y-scale.
  const allVals = [...b.values, ...s.values, ...nz.values, ...indVals, 0];
  if (wiSeries) allVals.push(...wiSeries.values);
  if (blSeries) allVals.push(...blSeries.values);
  const vmin = Math.min(...allVals);
  const vmax = Math.max(...allVals);
  const span = (vmax - vmin) || 1;
  const padL = 48, padR = 56, padT = 18, padB = 32;
  // FX band sits between the line chart and x-axis labels. Carved out of
  // the inner height so the line chart shrinks slightly instead of overlapping.
  const FX_H = fx.values.length ? 36 : 0;
  const FX_GAP = fx.values.length ? 10 : 0;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB - FX_H - FX_GAP;
  const fxTop = padT + innerH + FX_GAP;
  const fxBaseY = fxTop + FX_H / 2;       // baseline = midline of FX band

  function buildPoints(series) {
    if (!series.values.length) return {xs:[], ys:[], dates:[], vals:[]};
    const n = series.values.length;
    const xs = series.values.map((_, i) => padL + (n === 1 ? innerW/2 : (i/(n-1)) * innerW));
    const ys = series.values.map(v => padT + (1 - (v - vmin)/span) * innerH);
    return {xs, ys, dates: series.dates, vals: series.values};
  }
  const basket = buildPoints(b);
  const spy = buildPoints(s);
  const nasdaq = buildPoints(nz);

  // Y ticks (5 levels)
  const yTicks = [];
  for (let i = 0; i <= 5; i++) {
    const v = vmin + (i/5) * span;
    const y = padT + (1 - (v - vmin)/span) * innerH;
    yTicks.push({v, y});
  }
  // X ticks (~5; fewer on narrow/mobile charts so the date labels don't collide)
  const n = basket.xs.length;
  const xTickCount = Math.min(W < 460 ? 4 : 5, n);
  const xTicks = [];
  for (let i = 0; i < xTickCount; i++) {
    const idx = Math.round((i/(xTickCount-1)) * (n-1));
    xTicks.push({idx, x: basket.xs[idx], date: basket.dates[idx]});
  }

  // Align SPY's x grid to basket's so dates line up exactly. buildPoints
  // spreads each series across innerW based on its OWN length, so a 84-point
  // SPY ended ~1% offset from an 85-point basket even though their dates
  // matched 1:1. Re-mapping SPY's xs to basket's positions at matching dates
  // fixes that — both the rendered SPY polyline and the vs-SPY area edge now
  // sit on a single consistent x grid.
  // v1.8 T2: this remap must happen BEFORE building spyPL below, otherwise the
  // SPY polyline string captures the original xs while the vs-SPY polygons
  // (built later) use the remapped xs, producing a visible gap between them.
  if (spy.dates && spy.dates.length && spy.dates.length <= basket.dates.length) {
    const remapped = spy.dates.map(d => {
      const idx = basket.dates.indexOf(d);
      return idx >= 0 ? basket.xs[idx] : NaN;
    });
    if (remapped.every(v => !Number.isNaN(v))) spy.xs = remapped;
  }
  if (nasdaq.dates && nasdaq.dates.length && nasdaq.dates.length <= basket.dates.length) {
    const remappedN = nasdaq.dates.map(d => {
      const idx = basket.dates.indexOf(d);
      return idx >= 0 ? basket.xs[idx] : NaN;
    });
    if (remappedN.every(v => !Number.isNaN(v))) nasdaq.xs = remappedN;
  }

  const basketPL = basket.xs.map((x, i) => `${x.toFixed(1)},${basket.ys[i].toFixed(1)}`).join(' ');
  const spyPL = spy.xs.map((x, i) => `${x.toFixed(1)},${spy.ys[i].toFixed(1)}`).join(' ');
  const nasdaqPL = nasdaq.xs.map((x, i) => `${x.toFixed(1)},${nasdaq.ys[i].toFixed(1)}`).join(' ');
  const nasdaqColor = '#a78bfa';
  const nasdaqEnd = nasdaq.vals.length ? nasdaq.vals[nasdaq.vals.length - 1] : 0;
  const zeroY = padT + (1 - (0 - vmin)/span) * innerH;

  const basketEnd = basket.vals[basket.vals.length - 1];
  const spyEnd = spy.vals.length ? spy.vals[spy.vals.length - 1] : 0;
  const basketColor = '#f59e0b';
  const spyColor = '#6b7185';
  const whatifColor = '#2dd4bf';    // teal — distinct from the amber basket
  const blendedColor = '#c084fc';   // purple — basket blended with the selection
  const greenFill = 'rgba(52,211,153,0.22)';   // outperforming SPY
  const redFill = 'rgba(248,113,113,0.22)';    // underperforming SPY
  const lossWashFill = 'rgba(248,113,113,0.09)'; // subtle below-zero wash

  // Vs-SPY area segments: between basket and SPY lines, painted green when
  // basket is above SPY and red when below. Crossovers are split exactly via
  // linear interpolation on the difference so the segment edges land on the
  // true crossing point, not the nearest weekly tick.

  // Vs-SPY area segments: between basket and SPY lines, painted green when
  // basket > SPY and red when below. Iterate over the overlapping date range
  // (SPY can start a week later than basket since the benchmark series begins
  // from first-trading-day-after-first-purchase).
  // v1.8 T2 fix: pair basket and SPY by DATE (not by index). Previously the
  // loop paired basket.xs[startInBasket+k] with spy.ys[k], which mis-aligned
  // when basket had middle weeks SPY didn't. With the remap moved above
  // spyPL, spy.xs and basket.xs share one grid for matching dates, so the
  // polygon bottom edge follows the SPY polyline exactly.
  const vsSpySegments = [];
  if (spy.dates && spy.dates.length >= 2) {
    const basketIdxAt = spy.dates.map(d => basket.dates.indexOf(d));
    for (let k = 0; k < spy.dates.length - 1; k++) {
      const bi = basketIdxAt[k], bi2 = basketIdxAt[k + 1];
      if (bi < 0 || bi2 < 0) continue;
      const x1 = basket.xs[bi], x2 = basket.xs[bi2];
      const by1 = basket.ys[bi], by2 = basket.ys[bi2];
      const sy1 = spy.ys[k], sy2 = spy.ys[k + 1];
      const d1 = basket.vals[bi] - spy.vals[k];
      const d2 = basket.vals[bi2] - spy.vals[k + 1];
      if (d1 === 0 && d2 === 0) continue;
      if ((d1 >= 0) === (d2 >= 0)) {
        const color = (d1 + d2) >= 0 ? greenFill : redFill;
        vsSpySegments.push({
          pts: [[x1, by1], [x2, by2], [x2, sy2], [x1, sy1]],
          color,
        });
      } else {
        const t = d1 / (d1 - d2);
        const crossX = x1 + t * (x2 - x1);
        const crossY = by1 + t * (by2 - by1);
        vsSpySegments.push({
          pts: [[x1, by1], [crossX, crossY], [x1, sy1]],
          color: d1 >= 0 ? greenFill : redFill,
        });
        vsSpySegments.push({
          pts: [[crossX, crossY], [x2, by2], [x2, sy2]],
          color: d2 >= 0 ? greenFill : redFill,
        });
      }
    }
    // v1.8.1 B6: basket can extend past SPY's last date (weekend builds where
    // European tickers traded after SPY's Friday close, or holidays). Without
    // a forward-fill the shade has a visible gap at the right edge. Extend a
    // final segment from SPY's last paired basket-index up to basket's end,
    // holding SPY's last value flat. The dashed SPY polyline itself stays
    // visually honest (stops at its real last data point) -- only the
    // comparison shading is forward-filled.
    const lastSpyK = spy.dates.length - 1;
    const lastSpyBi = basketIdxAt[lastSpyK];
    if (lastSpyBi >= 0 && lastSpyBi < basket.xs.length - 1) {
      const sxL = basket.xs[lastSpyBi];
      const syL = spy.ys[lastSpyK];
      const spyTailVal = spy.vals[lastSpyK];
      for (let bi = lastSpyBi; bi < basket.xs.length - 1; bi++) {
        const x1 = basket.xs[bi], x2 = basket.xs[bi + 1];
        const by1 = basket.ys[bi], by2 = basket.ys[bi + 1];
        const d1 = basket.vals[bi] - spyTailVal;
        const d2 = basket.vals[bi + 1] - spyTailVal;
        if (d1 === 0 && d2 === 0) continue;
        if ((d1 >= 0) === (d2 >= 0)) {
          const color = (d1 + d2) >= 0 ? greenFill : redFill;
          vsSpySegments.push({
            pts: [[x1, by1], [x2, by2], [x2, syL], [x1, syL]],
            color,
          });
        } else {
          const t = d1 / (d1 - d2);
          const crossX = x1 + t * (x2 - x1);
          const crossY = by1 + t * (by2 - by1);
          vsSpySegments.push({
            pts: [[x1, by1], [crossX, crossY], [x1, syL]],
            color: d1 >= 0 ? greenFill : redFill,
          });
          vsSpySegments.push({
            pts: [[crossX, crossY], [x2, by2], [x2, syL]],
            color: d2 >= 0 ? greenFill : redFill,
          });
        }
      }
    }
  }

  // v2.7 #2: UK fiscal-year-start markers (~6 Apr each year). Map a calendar
  // date to x by interpolating between the basket's index-placed weekly points
  // so the marker lands exactly on the chart's own x grid (not a parallel one).
  function xForDate(targetMs) {
    const ds = basket.dates;
    if (!ds.length) return null;
    const d0 = Date.parse(ds[0] + 'T00:00:00');
    const dN = Date.parse(ds[ds.length - 1] + 'T00:00:00');
    if (targetMs < d0 || targetMs > dN) return null;   // outside range -> skip
    for (let i = 1; i < ds.length; i++) {
      const di = Date.parse(ds[i] + 'T00:00:00');
      if (targetMs <= di) {
        const dprev = Date.parse(ds[i - 1] + 'T00:00:00');
        const f = (targetMs - dprev) / Math.max(di - dprev, 1);
        return basket.xs[i - 1] + f * (basket.xs[i] - basket.xs[i - 1]);
      }
    }
    return basket.xs[basket.xs.length - 1];
  }
  let fyHtml = '';
  if (basket.dates.length) {
    const yr0 = new Date(basket.dates[0] + 'T00:00:00').getFullYear();
    const yr1 = new Date(basket.dates[basket.dates.length - 1] + 'T00:00:00').getFullYear();
    for (let yr = yr0; yr <= yr1; yr++) {
      const fyMs = Date.parse(yr + '-04-06T00:00:00');   // UK tax year starts 6 Apr
      const fxx = xForDate(fyMs);
      if (fxx == null) continue;
      const lbl = `FY${String(yr % 100).padStart(2, '0')}/${String((yr + 1) % 100).padStart(2, '0')}`;
      fyHtml += `<line x1="${fxx.toFixed(1)}" y1="${padT}" x2="${fxx.toFixed(1)}" y2="${(padT + innerH).toFixed(1)}" stroke="#6b7185" stroke-width="0.8" stroke-dasharray="2 4" opacity="0.45"/>`;
      fyHtml += `<text x="${(fxx + 3).toFixed(1)}" y="${(padT + 9).toFixed(1)}" fill="#6b7185" font-size="8.5" font-family="Geist Mono, monospace" opacity="0.85">${lbl}</text>`;
    }
  }
  // v2.7: last-COMPLETED UK fiscal year return (6 Apr -> 6 Apr), shown top-left.
  let fyStatHtml = '';
  if (basket.dates.length) {
    const valForDate = (targetMs) => {
      const ds = basket.dates;
      const d0 = Date.parse(ds[0] + 'T00:00:00');
      const dN = Date.parse(ds[ds.length - 1] + 'T00:00:00');
      if (targetMs < d0 || targetMs > dN) return null;
      for (let i = 1; i < ds.length; i++) {
        const di = Date.parse(ds[i] + 'T00:00:00');
        if (targetMs <= di) {
          const dp = Date.parse(ds[i - 1] + 'T00:00:00');
          const f = (targetMs - dp) / Math.max(di - dp, 1);
          return basket.vals[i - 1] + f * (basket.vals[i] - basket.vals[i - 1]);
        }
      }
      return basket.vals[basket.vals.length - 1];
    };
    const today = new Date();
    const fyEndYr = (today >= new Date(today.getFullYear(), 3, 6))
                    ? today.getFullYear() : today.getFullYear() - 1;
    const vS = valForDate(Date.parse((fyEndYr - 1) + '-04-06T00:00:00'));
    const vE = valForDate(Date.parse(fyEndYr + '-04-06T00:00:00'));
    if (vS !== null && vE !== null) {
      const fyRet = ((1 + vE / 100) / (1 + vS / 100) - 1) * 100;
      const col = fyRet >= 0 ? '#34d399' : '#f87171';
      const lbl = `FY${String((fyEndYr - 1) % 100).padStart(2, '0')}/${String(fyEndYr % 100).padStart(2, '0')}`;
      fyStatHtml = `<text x="${(padL + 5).toFixed(1)}" y="${(padT + 11).toFixed(1)}" font-family="Geist Mono, monospace" font-size="11" font-weight="600"><tspan fill="#6b7185">${lbl} </tspan><tspan fill="${col}">${fyRet >= 0 ? '+' : ''}${fyRet.toFixed(1)}%</tspan><title>Basket return over the last completed UK fiscal year (6 Apr ${fyEndYr - 1} to 6 Apr ${fyEndYr})</title></text>`;
    }
  }

  let html = '';
  // Y grid (with axis labels)
  html += yTicks.map(t =>
    `<line x1="${padL}" y1="${t.y.toFixed(1)}" x2="${padL + innerW}" y2="${t.y.toFixed(1)}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>` +
    `<text x="${padL - 8}" y="${(t.y + 3.5).toFixed(1)}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="end">${t.v >= 0 ? '+' : ''}${t.v.toFixed(0)}%</text>`
  ).join('');
  // Loss-zone wash: subtle red rectangle from the zero line down to the chart
  // floor so "below baseline" reads at a glance, even before reading any number.
  const chartBottom = padT + innerH;
  if (zeroY < chartBottom) {
    html += `<rect x="${padL}" y="${zeroY.toFixed(1)}" width="${innerW.toFixed(1)}" height="${(chartBottom - zeroY).toFixed(1)}" fill="${lossWashFill}"/>`;
  }
  // Vs-SPY area segments (paint *before* the zero line + lines so they stay crisp).
  // v3.4 #3: hidden in the short-term view (since-inception decoration).
  if (isAll) {
    html += vsSpySegments.map(seg =>
      `<polygon points="${seg.pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')}" fill="${seg.color}"/>`
    ).join('');
  }
  // Zero line — bumped from 0.18 to 0.34 alpha + 1.2 stroke so it's clearly
  // the "Oct '24 baseline" reference, not just another gridline.
  html += `<line x1="${padL}" y1="${zeroY.toFixed(1)}" x2="${padL + innerW}" y2="${zeroY.toFixed(1)}" stroke="rgba(255,255,255,0.34)" stroke-width="1.2" stroke-dasharray="4 3"/>`;
  // X labels — positioned below the FX band (or below the line chart when no FX)
  const xLabelY = padT + innerH + FX_GAP + FX_H + 16;
  html += xTicks.map(t =>
    `<text x="${t.x.toFixed(1)}" y="${xLabelY.toFixed(1)}" fill="#6b7185" font-size="10" font-family="Geist Mono, monospace" text-anchor="middle">${fmtAxisDate(t.date)}</text>`
  ).join('');
  // v2.7 #2: UK FY-start vertical markers (drawn under the data lines).
  html += fyHtml;
  // SPY line (dashed)
  if (spy.xs.length) {
    html += `<polyline points="${spyPL}" fill="none" stroke="${spyColor}" stroke-width="1.4" stroke-dasharray="4 3" stroke-linejoin="round"/>`;
  }
  // v3.0 #3: Nasdaq (QQQ) overlay — dotted, drawn under the basket line so the
  // basket stays visually dominant. Only present when the legend toggle is on.
  if (nasdaq.xs.length) {
    html += `<polyline points="${nasdaqPL}" fill="none" stroke="${nasdaqColor}" stroke-width="1.4" stroke-dasharray="1 3" stroke-linejoin="round"/>`;
  }
  // v3.4 #4: industry overlay — owned trajectory lines + non-owned 12m markers.
  // Drawn under the basket line so the basket stays dominant. Toggle-gated.
  if (ind.length) {
    ind.forEach(e => {
      if (e.series && e.series.values.length) {
        const p = buildPoints(e.series);
        if (p.dates && p.dates.length && p.dates.length <= basket.dates.length) {
          const rem = p.dates.map(d => { const i = basket.dates.indexOf(d); return i >= 0 ? basket.xs[i] : NaN; });
          if (rem.every(v => !Number.isNaN(v))) p.xs = rem;
        }
        const pl = p.xs.map((x, i) => `${x.toFixed(1)},${p.ys[i].toFixed(1)}`).join(' ');
        html += `<polyline points="${pl}" fill="none" stroke="${e.color}" stroke-width="1.4" stroke-linejoin="round" opacity="0.9"/>`;
      } else if (e.endpoint != null) {
        const rawY = padT + (1 - (e.endpoint - vmin) / span) * innerH;
        const my = Math.max(padT + 5, Math.min(padT + innerH - 5, rawY));
        const mx = padL + innerW;
        html += `<path d="M${(mx-4).toFixed(1)},${my.toFixed(1)} L${mx.toFixed(1)},${(my-4).toFixed(1)} L${(mx+4).toFixed(1)},${my.toFixed(1)} L${mx.toFixed(1)},${(my+4).toFixed(1)} Z" fill="none" stroke="${e.color}" stroke-width="1.4"/>`;
        const sgn = e.endpoint >= 0 ? '+' : '';
        html += `<text x="${(mx-8).toFixed(1)}" y="${(my+3).toFixed(1)}" text-anchor="end" fill="${e.color}" font-size="8.5" font-family="Geist Mono, monospace">${sgn}${Math.round(e.endpoint)}% 12m</text>`;
      }
    });
  }
  // v3.6: what-if (dashed) + blended (dotted) lines, drawn under the basket.
  let wiPts = null, blPts = null;
  if (wiSeries) {
    wiPts = buildPoints(wiSeries);
    const pl = wiPts.xs.map((x, i) => `${x.toFixed(1)},${wiPts.ys[i].toFixed(1)}`).join(' ');
    html += `<polyline points="${pl}" fill="none" stroke="${whatifColor}" stroke-width="2" stroke-dasharray="7 5" stroke-linejoin="round"/>`;
  }
  if (blSeries) {
    blPts = buildPoints(blSeries);
    const pl = blPts.xs.map((x, i) => `${x.toFixed(1)},${blPts.ys[i].toFixed(1)}`).join(' ');
    html += `<polyline points="${pl}" fill="none" stroke="${blendedColor}" stroke-width="1.8" stroke-dasharray="2 4" stroke-linejoin="round"/>`;
  }
  // Basket line
  html += `<polyline points="${basketPL}" fill="none" stroke="${basketColor}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`;
  // v2.7: last-completed fiscal-year return (top-left, on top of the line).
  if (isAll) html += fyStatHtml;   // v3.4 #3: since-inception only

  // End labels (basket + SPY) + vs-SPY delta badge
  html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(basket.ys[n-1] + 4).toFixed(1)}" fill="${basketColor}" font-size="11" font-family="Geist Mono, monospace" font-weight="500">${basketEnd >= 0 ? '+' : ''}${basketEnd.toFixed(1)}%</text>`;
  if (spy.ys.length) {
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(spy.ys[spy.ys.length-1] + 4).toFixed(1)}" fill="${spyColor}" font-size="11" font-family="Geist Mono, monospace">${spyEnd >= 0 ? '+' : ''}${spyEnd.toFixed(1)}%</text>`;
    // Vs-SPY delta in percentage points, positioned BELOW both end labels
    // (use the max y = the lower of the two lines, plus 14px offset).
    const vsDelta = basketEnd - spyEnd;
    const vsColor = vsDelta >= 0 ? '#34d399' : '#f87171';
    const vsY = Math.max(basket.ys[n-1], spy.ys[spy.ys.length-1]) + 16;
    // Δ is the "delta" — implies "basket minus SPY" without spelling it out.
    // Keeps the badge within the chart's right padding (~50px).
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${vsY.toFixed(1)}" fill="${vsColor}" font-size="10.5" font-family="Geist Mono, monospace" font-weight="600">&#916; ${vsDelta >= 0 ? '+' : ''}${vsDelta.toFixed(1)}pp</text>`;
  }
  if (nasdaq.ys.length) {
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(nasdaq.ys[nasdaq.ys.length-1] + 4).toFixed(1)}" fill="${nasdaqColor}" font-size="11" font-family="Geist Mono, monospace">${nasdaqEnd >= 0 ? '+' : ''}${nasdaqEnd.toFixed(1)}%</text>`;
  }
  // v3.6: what-if + blended end labels (window Δ)
  if (wiPts && wiPts.ys.length) {
    const ev = wiSeries.values[wiSeries.values.length - 1];
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(wiPts.ys[wiPts.ys.length-1] + 4).toFixed(1)}" fill="${whatifColor}" font-size="11" font-family="Geist Mono, monospace" font-weight="500">${ev >= 0 ? '+' : ''}${ev.toFixed(1)}%</text>`;
  }
  if (blPts && blPts.ys.length) {
    const ev = blSeries.values[blSeries.values.length - 1];
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(blPts.ys[blPts.ys.length-1] + 4).toFixed(1)}" fill="${blendedColor}" font-size="11" font-family="Geist Mono, monospace">${ev >= 0 ? '+' : ''}${ev.toFixed(1)}%</text>`;
  }

  // FX bar band — weekly GBP/USD rate, centered on the baseline (first value).
  // Up bar = stronger GBP, down bar = weaker GBP. Color follows --up / --down.
  // Baseline + delta arrays declared at outer scope so the hover handler below
  // can resolve the FX value at the hovered week index.
  const fxBaseline = fx.values.length ? (fx.values[0] || 1) : 1;
  const fxDeltas = fx.values.map(v => (v / fxBaseline - 1) * 100);
  if (fx.values.length) {
    const fxMaxAbs = Math.max(0.5, ...fxDeltas.map(Math.abs));         // floor to avoid tiny bars
    const fxMin = Math.min(...fx.values);
    const fxMax = Math.max(...fx.values);
    const fxN = fx.values.length;
    const slotW = innerW / fxN;
    const barW = Math.max(2, slotW * 0.55);
    const fxXs = fx.values.map((_, i) => padL + ((fxN === 1 ? innerW/2 : (i/(fxN-1)) * innerW)));
    const fxBars = fxDeltas.map((d, i) => {
      const half = (Math.abs(d) / fxMaxAbs) * (FX_H / 2 - 1);
      const y = d >= 0 ? fxBaseY - half : fxBaseY;
      const h = half;
      const fill = d >= 0 ? '#34d399' : '#f87171';
      return `<rect x="${(fxXs[i] - barW/2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${fill}" opacity="0.55" data-i="${i}"/>`;
    }).join('');
    // Baseline midline + label (shows the reference value the bars deviate from)
    html += `<line x1="${padL}" y1="${fxBaseY.toFixed(1)}" x2="${padL + innerW}" y2="${fxBaseY.toFixed(1)}" stroke="rgba(255,255,255,0.10)" stroke-width="0.7"/>`;
    html += fxBars;
    // Left axis: label + the actual baseline rate in $/£ so bar magnitudes are interpretable
    html += `<text x="${padL - 8}" y="${(fxBaseY - 1).toFixed(1)}" fill="#6b7185" font-size="9" font-family="Geist Mono, monospace" text-anchor="end">GBP/$</text>`;
    html += `<text x="${padL - 8}" y="${(fxBaseY + 9).toFixed(1)}" fill="#6b7185" font-size="8.5" font-family="Geist Mono, monospace" text-anchor="end">ref $${fxBaseline.toFixed(3)}</text>`;
    // Range labels at top/bottom of the FX strip — show $min and $max
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(fxTop + 4).toFixed(1)}" fill="#34d399" font-size="8.5" font-family="Geist Mono, monospace">$${fxMax.toFixed(3)}</text>`;
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(fxTop + FX_H - 1).toFixed(1)}" fill="#f87171" font-size="8.5" font-family="Geist Mono, monospace">$${fxMin.toFixed(3)}</text>`;
    // Current value (slightly larger) at end of baseline
    const fxEnd = fx.values[fx.values.length - 1];
    const fxEndDelta = fxDeltas[fxDeltas.length - 1];
    const fxColor = fxEndDelta >= 0 ? '#34d399' : '#f87171';
    html += `<text x="${(padL + innerW + 6).toFixed(1)}" y="${(fxBaseY + 3).toFixed(1)}" fill="${fxColor}" font-size="10" font-family="Geist Mono, monospace" font-weight="500">$${fxEnd.toFixed(3)}</text>`;
  }

  // Crosshair + hover dots. pointer-events="none" so they don't intercept
  // clicks on the T15 week-click rects layered above them in DOM order.
  html += `<line class="hero-cross" x1="0" y1="${padT}" x2="0" y2="${padT + innerH}" stroke="${basketColor}" stroke-width="0.8" stroke-dasharray="2 3" opacity="0" pointer-events="none"/>`;
  html += `<circle class="hero-dot-basket" cx="0" cy="0" r="4" fill="${basketColor}" opacity="0" pointer-events="none"/>`;
  html += `<circle class="hero-dot-spy" cx="0" cy="0" r="3.5" fill="${spyColor}" opacity="0" pointer-events="none"/>`;

  // T15: per-week transparent click rects. One <rect> per weekly point spans
  // half the gap before + after that point so clicks land naturally near the
  // visible value. data-week-end carries the week-ending date used to look
  // up AUX_DATA.weekly_movers in the click handler. pointer-events="all" is
  // explicit because transparent-fill SVG rects can be hit-test-skipped by
  // some browsers in edge cases.
  if (basket.xs.length > 1) {
    const halfStep = (basket.xs[1] - basket.xs[0]) / 2;
    for (let i = 0; i < basket.xs.length; i++) {
      const cx = basket.xs[i];
      const left  = Math.max(padL, cx - halfStep);
      const right = Math.min(padL + innerW, cx + halfStep);
      const w = right - left;
      if (w <= 0) continue;
      html += `<rect class="hero-week-click" data-week-end="${basket.dates[i]}" `
            + `x="${left.toFixed(1)}" y="${padT}" width="${w.toFixed(1)}" `
            + `height="${innerH.toFixed(1)}" fill="transparent" pointer-events="all" />`;
    }
  }

  svg.innerHTML = html;

  // Hover handler
  svg.onmousemove = (e) => {
    const rect = svg.getBoundingClientRect();
    const xpx = (e.clientX - rect.left) * (W / rect.width);
    // Find nearest basket index
    let bestI = 0, bestDist = Infinity;
    for (let i = 0; i < basket.xs.length; i++) {
      const d = Math.abs(basket.xs[i] - xpx);
      if (d < bestDist) { bestDist = d; bestI = i; }
    }
    const bx = basket.xs[bestI], by = basket.ys[bestI];
    const bv = basket.vals[bestI];
    // Find SPY value at same date (by index — they're both weekly, aligned closely)
    let sv = null, sy = 0;
    if (spy.dates.length) {
      const target = basket.dates[bestI];
      let sIdx = spy.dates.indexOf(target);
      if (sIdx < 0) {
        // Approximate by index ratio
        sIdx = Math.min(Math.round(bestI * (spy.dates.length-1) / Math.max(basket.dates.length-1, 1)), spy.dates.length - 1);
      }
      sv = spy.vals[sIdx];
      sy = spy.ys[sIdx];
    }
    const cross = svg.querySelector('.hero-cross');
    const dotB = svg.querySelector('.hero-dot-basket');
    const dotS = svg.querySelector('.hero-dot-spy');
    cross.setAttribute('x1', bx); cross.setAttribute('x2', bx); cross.setAttribute('opacity', '0.7');
    dotB.setAttribute('cx', bx); dotB.setAttribute('cy', by); dotB.setAttribute('opacity', '1');
    if (sv !== null) {
      dotS.setAttribute('cx', bx); dotS.setAttribute('cy', sy); dotS.setAttribute('opacity', '1');
    }
    const tipX = (bx / W) * rect.width;
    const tipY = (by / H) * rect.height;
    tip.style.left = tipX + 'px';
    tip.style.top = tipY + 'px';
    // Match FX value at the same week index when possible. FX series may be
    // shorter (no fx data for very first weeks); fall back to nearest index.
    let fxAtVal = null, fxAtDelta = null;
    if (fx.values.length) {
      const fxTarget = basket.dates[bestI];
      let fxIdx = fx.dates.indexOf(fxTarget);
      if (fxIdx < 0) fxIdx = Math.min(bestI, fx.values.length - 1);
      fxAtVal = fx.values[fxIdx];
      fxAtDelta = fxDeltas[fxIdx];
    }
    tip.innerHTML =
      `<div class="tip-date">${fmtDate(basket.dates[bestI])}</div>` +
      `<div class="tip-row"><span class="tip-label">Basket</span><span class="${bv >= 0 ? 'pos' : 'neg'}">${bv >= 0 ? '+' : ''}${bv.toFixed(2)}%</span></div>` +
      (sv !== null ? `<div class="tip-row"><span class="tip-label">SPY</span><span class="${sv >= 0 ? 'pos' : 'neg'}">${sv >= 0 ? '+' : ''}${sv.toFixed(2)}%</span></div>` : '') +
      (fxAtVal !== null ? `<div class="tip-row"><span class="tip-label">GBP/USD</span><span class="${fxAtDelta >= 0 ? 'pos' : 'neg'}">$${fxAtVal.toFixed(3)} (${fxAtDelta >= 0 ? '+' : ''}${fxAtDelta.toFixed(2)}%)</span></div>` : '');
    tip.removeAttribute('hidden');
  };
  svg.onmouseleave = () => {
    tip.setAttribute('hidden', '');
    const c = svg.querySelector('.hero-cross');
    if (c) c.setAttribute('opacity', '0');
    svg.querySelectorAll('circle').forEach(d => d.setAttribute('opacity', '0'));
  };
}

renderHeroChart();
window.addEventListener('resize', renderHeroChart);
// v3.0 #3: Nasdaq overlay legend toggle (default off, persisted in localStorage).
(function() {
  const btn = document.querySelector('.hero-legend .leg-toggle[data-series="nasdaq"]');
  if (!btn) return;
  btn.setAttribute('aria-pressed', showNasdaq ? 'true' : 'false');
  btn.addEventListener('click', () => {
    showNasdaq = !showNasdaq;
    try { localStorage.setItem('heroNasdaq', showNasdaq ? '1' : '0'); } catch (e) {}
    btn.setAttribute('aria-pressed', showNasdaq ? 'true' : 'false');
    renderHeroChart();
  });
})();
// v3.4 #4: Industry overlay legend toggle (default off, persisted). Shows/hides
// the owned industry lines + non-owned 12m markers + the name legend group.
(function() {
  const btn = document.querySelector('.hero-legend .leg-toggle[data-series="industries"]');
  if (!btn) return;
  const grp = document.querySelector('.hero-legend .hero-ind-legend');
  btn.setAttribute('aria-pressed', showIndustries ? 'true' : 'false');
  if (grp) grp.hidden = !showIndustries;
  btn.addEventListener('click', () => {
    showIndustries = !showIndustries;
    try { localStorage.setItem('heroIndustries', showIndustries ? '1' : '0'); } catch (e) {}
    btn.setAttribute('aria-pressed', showIndustries ? 'true' : 'false');
    if (grp) grp.hidden = !showIndustries;
    renderHeroChart();
  });
})();

// v3.4 #3: short-term range control (All / 3M / 1M). Transient (not persisted).
(function() {
  const wrap = document.querySelector('.hero-chart-wrap');
  const btns = document.querySelectorAll('.hero-range .hero-range-btn');
  if (!btns.length) return;
  btns.forEach(btn => btn.addEventListener('click', () => {
    const r = btn.dataset.range;
    if (r === heroRange) return;
    heroRange = r;
    btns.forEach(b2 => b2.setAttribute('aria-pressed', b2.dataset.range === r ? 'true' : 'false'));
    if (wrap) {
      wrap.classList.toggle('range-short', r !== 'all');
      wrap.classList.add('range-anim');
      setTimeout(() => { renderHeroChart(); wrap.classList.remove('range-anim'); renderWhatIfChips(); }, 120);
    } else { renderHeroChart(); renderWhatIfChips(); }
  }));
})();

// v3.6 what-if: legend toggles + watchlist-checkbox sync + chip echo. Chips show
// only in short mode when a line is on; unflagged/aged names render dimmed/dead.
function renderWhatIfChips() {
  const box = document.getElementById('whatif-chips');
  const note = document.getElementById('whatif-note');
  if (!box) return;
  const wi = whatIfData();
  const show = (whatIfOn || whatIfBlendedOn) && heroRange !== 'all';
  box.hidden = !show;
  if (note) note.hidden = !show;
  if (!show) { box.innerHTML = ''; return; }
  const frag = [];
  for (const t of whatIfSel) {
    const d = wi.names[t];
    let cls = 'whatif-chip', label = t;
    if (!d) { cls += ' dead'; label = t + ' · no longer tracked'; }
    else if (!d.flagged_now) {
      cls += ' unflagged';
      let days = null;
      if (d.last_flagged) { const dt = Date.parse(d.last_flagged); if (!isNaN(dt)) days = Math.round((Date.now() - dt) / 86400000); }
      label = t + (days !== null ? ' · unflagged ' + days + 'd ago' : ' · unflagged');
    }
    frag.push('<button type="button" class="' + cls + '" data-wi-remove="' + t + '">' + label + '<span class="x">×</span></button>');
  }
  const addable = Object.keys(wi.names || {}).filter(t => whatIfSel.indexOf(t) < 0);
  if (addable.length) frag.push('<button type="button" class="whatif-add" id="whatif-add">+ add</button>');
  box.innerHTML = frag.join('');
  const addBtn = document.getElementById('whatif-add');
  if (addBtn) addBtn.addEventListener('click', () => {
    const existing = box.querySelector('.whatif-pick');
    if (existing) { existing.remove(); return; }
    const pick = document.createElement('div');
    pick.className = 'whatif-pick';
    pick.innerHTML = addable.map(t => '<button type="button" class="whatif-add" data-wi-add="' + t + '">' + t + '</button>').join('');
    box.appendChild(pick);
  });
}
function whatIfPersistSel() { try { localStorage.setItem('whatIfSelection', JSON.stringify(whatIfSel)); } catch (e) {} }
function whatIfSyncCheckboxes() {
  document.querySelectorAll('[data-wi-ticker]').forEach(cb => {
    cb.checked = whatIfSel.indexOf(cb.getAttribute('data-wi-ticker')) >= 0;
  });
}
(function setupWhatIf() {
  document.querySelectorAll('.hero-legend .leg-toggle.leg-wi').forEach(btn => {
    const series = btn.dataset.series;   // 'whatif' | 'blended'
    const get = () => series === 'blended' ? whatIfBlendedOn : whatIfOn;
    btn.setAttribute('aria-pressed', get() ? 'true' : 'false');
    btn.addEventListener('click', () => {
      if (series === 'blended') whatIfBlendedOn = !whatIfBlendedOn; else whatIfOn = !whatIfOn;
      try { localStorage.setItem(series === 'blended' ? 'whatIfBlendedOn' : 'whatIfOn', get() ? '1' : '0'); } catch (e) {}
      btn.setAttribute('aria-pressed', get() ? 'true' : 'false');
      renderHeroChart();
      renderWhatIfChips();
    });
  });
  whatIfSyncCheckboxes();
  // The what-if checkbox sits inside a .wl-card whose own click opens the stock
  // modal — stop the tick from bubbling up so it doesn't also open the card.
  document.querySelectorAll('.wl-wi-pick').forEach(lbl => {
    lbl.addEventListener('click', (e) => e.stopPropagation());
  });
  document.addEventListener('change', (e) => {
    const cb = e.target;
    if (!cb || typeof cb.getAttribute !== 'function' || cb.getAttribute('data-wi-ticker') == null) return;
    const t = cb.getAttribute('data-wi-ticker');
    const i = whatIfSel.indexOf(t);
    if (cb.checked && i < 0) whatIfSel.push(t);
    else if (!cb.checked && i >= 0) whatIfSel.splice(i, 1);
    whatIfPersistSel();
    renderHeroChart();
    renderWhatIfChips();
  });
  const box = document.getElementById('whatif-chips');
  if (box) box.addEventListener('click', (e) => {
    const rm = e.target.closest ? e.target.closest('[data-wi-remove]') : null;
    const ad = e.target.closest ? e.target.closest('[data-wi-add]') : null;
    if (rm) {
      const t = rm.getAttribute('data-wi-remove');
      const i = whatIfSel.indexOf(t); if (i >= 0) whatIfSel.splice(i, 1);
      whatIfPersistSel(); whatIfSyncCheckboxes(); renderHeroChart(); renderWhatIfChips();
    } else if (ad) {
      const t = ad.getAttribute('data-wi-add');
      if (whatIfSel.indexOf(t) < 0) whatIfSel.push(t);
      whatIfPersistSel(); whatIfSyncCheckboxes(); renderHeroChart(); renderWhatIfChips();
    }
  });
  renderWhatIfChips();
})();

// ---- Stagger animations
document.querySelectorAll('#ret-table tbody tr').forEach((row, i) => {
  row.style.animationDelay = Math.min(i * 7, 700) + 'ms';
});
document.querySelectorAll('.card').forEach((c, i) => {
  c.style.animationDelay = Math.min(i * 5, 600) + 'ms';
});

// ---- Sort
let sortState = {col: -1, asc: false};
// Shared sort so both header clicks and per-mode defaults can set direction
// explicitly (v3.4 #6: losers/bottom10 need a forced ascending "worst first").
function sortTable(col, asc) {
  const th = document.querySelector(`#ret-table th[data-col="${col}"]`);
  const numeric = !!(th && th.dataset.num === '1');
  const tbody = document.querySelector('#ret-table tbody');
  if (!tbody) return;
  const rows = [...tbody.querySelectorAll('tr')];
  rows.sort((a, b) => {
    const ac = a.cells[col], bc = b.cells[col];
    const av = numeric ? parseFloat(ac.dataset.v ?? ac.textContent.replace(/[^-\d.]/g,'')) : ac.textContent;
    const bv = numeric ? parseFloat(bc.dataset.v ?? bc.textContent.replace(/[^-\d.]/g,'')) : bc.textContent;
    return (av < bv ? -1 : av > bv ? 1 : 0) * (asc ? 1 : -1);
  });
  rows.forEach(r => tbody.appendChild(r));
  sortState.col = col;
  sortState.asc = asc;
  document.querySelectorAll('#ret-table th').forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
  if (th) th.classList.add(asc ? 'sort-asc' : 'sort-desc');
}
document.querySelectorAll('#ret-table th[data-col]').forEach(th => {
  th.addEventListener('click', (e) => {
    e.stopPropagation();
    const col = +th.dataset.col;
    const asc = (sortState.col === col) ? !sortState.asc : false;
    sortTable(col, asc);
  });
});
sortTable(9, false);

// ---- Filtering
const TOTALS = Object.fromEntries(Object.entries(DATA).map(([t, d]) => [t, d.total]));
const WEIGHTS = Object.fromEntries(Object.entries(DATA).map(([t, d]) => [t, d.weight]));

function applyFilter(panel) {
  const search = (panel.querySelector('.search')?.value || '').trim().toUpperCase();
  // Status filter and sector filter live in two separate `.chips` rows so
  // each axis has its own active chip. Read both.
  const activeStatus = panel.querySelector('.chips:not(.chips-sectors) .chip.active');
  const mode = activeStatus ? activeStatus.dataset.filter : 'all';
  const activeSector = panel.querySelector('.chips-sectors .chip.active');
  const sector = activeSector ? activeSector.dataset.sector : '*';
  const sorted = Object.entries(TOTALS).sort((a, b) => b[1] - a[1]);
  let allowed = null;
  if (mode === 'top10') allowed = new Set(sorted.slice(0, 10).map(([t]) => t));
  else if (mode === 'bottom10') allowed = new Set(sorted.slice(-10).map(([t]) => t));

  const items = panel.querySelectorAll('#ret-table tbody tr');
  items.forEach(el => {
    const t = el.dataset.ticker;
    const total = parseFloat(el.dataset.total);
    const weight = parseFloat(el.dataset.weight) || 0;
    const rowSector = el.dataset.sector || '';
    let show = true;
    if (search && !t.includes(search)) show = false;
    if (mode === 'basket' && weight <= 0) show = false;
    if (mode === 'closed' && weight > 0) show = false;
    if (mode === 'winners' && total < 0) show = false;
    if (mode === 'losers' && total >= 0) show = false;
    if (allowed && !allowed.has(t)) show = false;
    if (sector !== '*' && rowSector !== sector) show = false;
    el.classList.toggle('hidden', !show);
  });
}

document.querySelectorAll('.panel').forEach(panel => {
  panel.querySelectorAll('.chip').forEach(chip => {
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      const group = chip.closest('.chips');
      group.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      applyFilter(panel);
      // v2.1 #3 / v3.4 #6: Apply default sort for the new status-filter mode.
      // Losers / Bottom 10 read as a "worst first" list -> ascending on
      // Since-baseline (biggest loss on top). Closed positions default to
      // Signal (col 4) so like-signal rows group together. Every other mode
      // falls back to Since-baseline descending (best first). Direction is set
      // explicitly rather than toggled, so switching modes is deterministic.
      // Only fires for status chips -- sector-filter chips keep current sort.
      if (!group.classList.contains('chips-sectors')) {
        const newMode = chip.dataset.filter;
        if (newMode === 'losers' || newMode === 'bottom10') sortTable(9, true);
        else if (newMode === 'closed') sortTable(4, false);
        else sortTable(9, false);
      }
      // v1.8.1 B5: reset scroll to top when filter changes -- otherwise the
      // user is mid-list in "Open" and switching to "Closed" leaves them at
      // an arbitrary scroll position in a now-different dataset.
      const scrollWrap = panel.querySelector('.table-scroll');
      if (scrollWrap) scrollWrap.scrollTop = 0;
    });
  });
  const s = panel.querySelector('.search');
  if (s) {
    s.addEventListener('input', () => {
      applyFilter(panel);
      const scrollWrap = panel.querySelector('.table-scroll');
      if (scrollWrap) scrollWrap.scrollTop = 0;
    });
    s.addEventListener('click', (e) => e.stopPropagation());
  }
});

// Default to showing only Open positions — that's the actionable view for
// most sessions. User can still click All to see closed too.
document.querySelector('.chip[data-filter="basket"]')?.click();

// ---- Modal
const modal = document.getElementById('modal');
const modalSvg = modal.querySelector('.modal-chart');
const modalTip = modal.querySelector('.modal-tip');
const modalChartWrap = modal.querySelector('.modal-chart-wrap');

let currentTicker = null;
let chartPoints = null;

async function openModal(ticker) {
  let d = DATA[ticker];
  if (!d) return;
  // v2.0 lazy-modal: if HEAVY hasn't been fetched/merged into this ticker yet,
  // open the modal with a light-only header + chart-area spinner, then await
  // the payload. On the COMMON path (prefetch completed during idle), this
  // resolves synchronously and the user sees no delay. On the cold path
  // (~400-650ms on 4G), the spinner makes the wait feel intentional.
  if (HEAVY_URL && !d.__hydrated) {
    modal.querySelector('.modal-ticker').textContent = ticker;
    modal.querySelector('.modal-name').textContent = d.name || ticker;
    modal.querySelector('.modal-industry').textContent = d.industry || d.sector || '';
    const _pctEarly = modal.querySelector('.modal-pct');
    _pctEarly.textContent = fmtPct(d.total, true);
    _pctEarly.className = 'modal-pct ' + (d.total >= 0 ? 'pos' : 'neg');
    const loadingEl = modal.querySelector('.modal-loading');
    if (loadingEl) loadingEl.hidden = false;
    modal.removeAttribute('hidden');
    document.body.classList.add('modal-open');
    const heavy = await loadHeavy();
    // Only mark hydrated when the fetch SUCCEEDED. loadHeavy() returns null
    // only on fetch failure (e.g. a GitHub Pages redeploy window); leaving
    // __hydrated unset in that case lets reopening the modal retry, instead
    // of poisoning the ticker with a permanent light-only (blank) modal.
    if (heavy) {
      if (heavy[ticker]) Object.assign(DATA[ticker], heavy[ticker]);
      DATA[ticker].__hydrated = true;   // payload may simply lack this ticker -- fine
    }
    d = DATA[ticker];
    if (loadingEl) loadingEl.hidden = true;
  }
  const tickerEl = modal.querySelector('.modal-ticker');
  const ccyBadge = (d.currency && d.currency !== BASE_CCY) ?
    ` <span class="badge-ccy">${d.currency}</span>` : '';
  let weightBadge;
  if (d.status === 'open') {
    weightBadge = ` <span class="badge-weight" title="${d.shares_held} units (1 unit per trade entry)">${d.shares_held} u</span>`;
  } else if (d.status === 'watch') {
    weightBadge = ' <span class="badge-watch" title="On watchlist, not held">WATCH</span>';
  } else {
    weightBadge = ' <span class="badge-closed">CLOSED</span>';
  }
  tickerEl.innerHTML = ticker + ccyBadge + weightBadge;
  modal.querySelector('.modal-name').textContent = d.name || ticker;
  modal.querySelector('.modal-industry').textContent = d.industry || d.sector || '';
  const pct = modal.querySelector('.modal-pct');
  pct.textContent = fmtPct(d.total, true);
  pct.className = 'modal-pct ' + (d.total >= 0 ? 'pos' : 'neg');
  // v1.8.1 B4: closed positions show the hold window, not "since [buy]".
  // The % is buy-avg→sell-avg, so the label should reflect that range so
  // users don't read it as a current-date return.
  let sinceLabel;
  if (d.status === 'watch') {
    sinceLabel = 'last 12 months';
  } else if (d.status === 'closed' && d.last_action_date) {
    sinceLabel = 'between ' + fmtDate(d.baseline_date) + ' and ' + fmtDate(d.last_action_date);
  } else {
    sinceLabel = 'since ' + fmtDate(d.baseline_date);
  }
  modal.querySelector('.modal-since').textContent = sinceLabel;
  // Signal line
  const sigEl = modal.querySelector('#modal-signal');
  if (d.signal) {
    sigEl.querySelector('.modal-signal-text').textContent = d.signal;
    sigEl.querySelector('.modal-signal-text').className = 'modal-signal-text ' + (d.signal_tone || 'neutral');
    sigEl.querySelector('.modal-signal-detail').textContent = d.signal_detail || '';
    sigEl.removeAttribute('hidden');
  } else {
    sigEl.setAttribute('hidden', '');
  }
  // FX attribution line — only for non-base-currency stocks
  const fxEl = modal.querySelector('#modal-fx');
  if (d.currency && d.currency !== BASE_CCY && d.native_total !== null && d.fx_change !== null) {
    const stockB = fxEl.querySelector('#fx-stock');
    const fxB = fxEl.querySelector('#fx-fx');
    const totalB = fxEl.querySelector('#fx-total');
    stockB.textContent = fmtPct(d.native_total, true) + ' (' + d.currency + ')';
    stockB.className = d.native_total >= 0 ? 'pos' : 'neg';
    fxB.textContent = fmtPct(d.fx_change, true);
    fxB.className = d.fx_change >= 0 ? 'pos' : 'neg';
    totalB.textContent = fmtPct(d.total, true) + ' (' + BASE_CCY + ')';
    totalB.className = d.total >= 0 ? 'pos' : 'neg';
    fxEl.removeAttribute('hidden');
  } else {
    fxEl.setAttribute('hidden', '');
  }
  const vals = {
    baseline: fmtMoney(d.baseline),
    latest: fmtMoney(d.latest),
    w1: fmtPct(d.w1, true),
    m1: fmtPct(d.m1, true),
    m3: fmtPct(d.m3, true),
    ytd: fmtPct(d.ytd, true),
  };
  modal.querySelectorAll('.modal-stat-val[data-key]').forEach(el => {
    const k = el.dataset.key;
    el.textContent = vals[k];
    el.className = 'modal-stat-val';
    if (k.match(/^(w1|m1|m3|ytd)$/) && d[k] !== null && d[k] !== undefined && !Number.isNaN(d[k])) {
      el.classList.add(d[k] >= 0 ? 'pos' : 'neg');
    }
  });
  // ---- Quant signals sub-row -----------------------------------------
  // SMA200 distance / ATR / RSI / 52w position / Volume. Falls back to
  // dim "—" when a metric couldn't be computed (e.g. <200 days history,
  // or no FX rate to convert ATR into base currency).
  const q = d.quant || {};
  const isNum = (v) => v !== null && v !== undefined && !Number.isNaN(v);
  const sma = isNum(q.sma200_dist_pct)
    ? ((q.sma200_dist_pct >= 0 ? '+' : '') + q.sma200_dist_pct.toFixed(1) + '%') : '—';
  const atr = isNum(q.atr14_gbp) ? (BASE_SYMBOL + q.atr14_gbp.toFixed(2)) : '—';
  const atrMeta = isNum(q.atr14_pct) ? (q.atr14_pct.toFixed(1) + '% of price') : '';
  const rsi = isNum(q.rsi14) ? q.rsi14.toFixed(0) : '—';
  const rsiMeta = isNum(q.rsi14)
    ? (q.rsi14 >= 70 ? 'overbought' : q.rsi14 <= 30 ? 'oversold' : 'neutral') : '';
  const rng = isNum(q.range52w_pct) ? (q.range52w_pct.toFixed(0) + '%') : '—';
  const rngMeta = isNum(q.range52w_pct)
    ? (q.range52w_pct >= 75 ? 'near high' : q.range52w_pct <= 25 ? 'near low' : 'mid-range') : '';
  const vol = isNum(q.vol_ratio) ? (q.vol_ratio.toFixed(1) + '×') : '—';
  const volMeta = isNum(q.vol_ratio) ? (q.vol_ratio >= 1.0 ? 'above avg' : 'below avg') : '';
  const qMap = {
    sma200_dist_pct: { val: sma, meta: '' },
    atr14_gbp:       { val: atr, meta: atrMeta },
    rsi14:           { val: rsi, meta: rsiMeta },
    range52w_pct:    { val: rng, meta: rngMeta },
    vol_ratio:       { val: vol, meta: volMeta },
  };
  // v2.1 #2: value-aware tooltip explaining each quant signal. Built from the
  // numeric value + threshold zone so the user sees "RSI 81 — overbought" not
  // just an abstract definition. Interpretation strings are generated here in
  // JS rather than baked per-ticker into HEAVY (saves ~5 KB × 187 tickers).
  function quantTitle(k, v) {
    if (!isNum(v)) return 'No data — typically <200 days of price history or missing FX rate.';
    if (k === 'sma200_dist_pct') {
      const dir = v >= 0 ? 'above' : 'below';
      return `Price is ${Math.abs(v).toFixed(1)}% ${dir} the 200-day moving average. ` +
             `Long-term trend ${v >= 0 ? 'up' : 'down'}; persistent moves below the SMA200 often signal sustained weakness.`;
    }
    if (k === 'atr14_gbp') {
      return `Average True Range over 14 days = ${BASE_SYMBOL}${v.toFixed(2)}. ` +
             `Typical daily price swing in absolute terms. A common stop-loss buffer is 2× ATR below entry for long positions.`;
    }
    if (k === 'rsi14') {
      const zone = v >= 70 ? 'Overbought (>70) — strong recent buying; momentum may exhaust soon.' :
                   v <= 30 ? 'Oversold (<30) — heavy recent selling; price may rebound.' :
                   'Neutral (30–70) — no clear momentum signal.';
      return `RSI(14) = ${v.toFixed(0)}. ${zone}`;
    }
    if (k === 'range52w_pct') {
      const zone = v >= 75 ? 'Near 52-week high — potential resistance or strong momentum signal.' :
                   v <= 25 ? 'Near 52-week low — potential support level or value zone.' :
                   'Mid-range.';
      return `At ${v.toFixed(0)}% of the 52-week price range (0% = year low, 100% = year high). ${zone}`;
    }
    if (k === 'vol_ratio') {
      const zone = v >= 2.0 ? 'Unusual — > 2× typical, often news-driven.' :
                   v >= 1.3 ? 'Elevated — above-average activity.' :
                   v >= 0.7 ? 'Roughly typical.' :
                   'Quiet — below-average activity.';
      return `Recent volume = ${v.toFixed(1)}× the 50-day average. ${zone}`;
    }
    return '';
  }
  modal.querySelectorAll('[data-qkey]').forEach(el => {
    const k = el.dataset.qkey;
    const entry = qMap[k];
    if (!entry) return;
    el.textContent = entry.val;
    el.className = 'modal-stat-val';
    const v = q[k];
    el.setAttribute('title', quantTitle(k, v));
    if (!isNum(v)) { el.classList.add('dim'); return; }
    if (k === 'sma200_dist_pct') el.classList.add(v >= 0 ? 'pos' : 'neg');
    else if (k === 'rsi14') {
      if (v >= 70) el.classList.add('neg');
      else if (v <= 30) el.classList.add('pos');
    } else if (k === 'range52w_pct') {
      if (v >= 75) el.classList.add('pos');
      else if (v <= 25) el.classList.add('neg');
    } else if (k === 'vol_ratio') {
      el.classList.add(v >= 1.0 ? 'pos' : 'neg');
    }
    // atr14_gbp stays neutral — it's a magnitude, not a direction.
  });
  modal.querySelectorAll('[data-qmeta]').forEach(el => {
    const k = el.dataset.qmeta;
    el.textContent = (qMap[k] && qMap[k].meta) || '';
  });
  // ---- Per-ticker recent news ----------------------------------------
  // Shown only when the build-time cache had items for this ticker. Empty
  // section is hidden entirely so the modal doesn't carry a dead row.
  const newsBox = document.getElementById('modal-news');
  const newsList = newsBox.querySelector('.modal-news-list');
  const newsStale = newsBox.querySelector('.modal-news-staleness');
  const items = Array.isArray(d.news) ? d.news : [];
  if (items.length) {
    newsList.innerHTML = items.map(it => {
      const safeTitle = escapeNewsHtml(it.title || '');
      const safeLink = safeUrl(it.link);
      const safePub = escapeNewsHtml(it.publisher || '');
      const when = it.published ? relativeNewsTime(new Date(it.published)) : '';
      const safeWhen = escapeNewsHtml(when);
      return `<a class="modal-news-row" href="${safeLink}" target="_blank" rel="noopener noreferrer">`
        + `<div class="modal-news-title">${safeTitle}</div>`
        + `<div class="modal-news-meta">`
        + (safePub ? `<span class="modal-news-pub">${safePub}</span>` : '')
        + (safePub && safeWhen ? `<span class="modal-news-dot">&middot;</span>` : '')
        + (safeWhen ? `<span class="modal-news-when">${safeWhen}</span>` : '')
        + `</div></a>`;
    }).join('');
    newsStale.textContent = 'cached weekly';
    newsBox.removeAttribute('hidden');
  } else {
    newsList.innerHTML = '';
    newsStale.textContent = '';
    newsBox.setAttribute('hidden', '');
  }
  modal.removeAttribute('hidden');
  document.body.classList.add('modal-open');
  // v1.8.1 B5: reset scroll so each new ticker opens at the top, not at the
  // previous ticker's last scroll position.
  const mc = modal.querySelector('.modal-card');
  if (mc) mc.scrollTop = 0;
  requestAnimationFrame(() => renderBigChart(ticker));
}

function closeModal() {
  modal.setAttribute('hidden', '');
  document.body.classList.remove('modal-open');
  modalTip.setAttribute('hidden', '');
}

// v1.8 T1: viewBox coordinate space — server pre-renders the polyline + tick
// geometry into `d.chart`, we just draw it. preserveAspectRatio="none" makes
// the browser scale the viewBox to the modal's actual pixel size, so no
// per-open recalc + no resize handler are needed.
const MODAL_VB_INNER_W = MODAL_VB_W - MODAL_VB_PAD_L - MODAL_VB_PAD_R;
const MODAL_VB_INNER_H = MODAL_VB_H - MODAL_VB_PAD_T - MODAL_VB_PAD_B;

// T1: derive per-point viewBox coords from `n` + index. Mirrors _modal_polyline_d.
function _modalX(i, n) {
  if (n <= 1) return MODAL_VB_PAD_L + MODAL_VB_INNER_W / 2;
  return MODAL_VB_PAD_L + (i / (n - 1)) * MODAL_VB_INNER_W;
}
function _modalY(v, vmin, vmax) {
  const span = Math.max(vmax - vmin, 1e-9);
  return MODAL_VB_PAD_T + (1 - (v - vmin) / span) * MODAL_VB_INNER_H;
}

function renderBigChart(ticker) {
  currentTicker = ticker;
  // v2.1 #1: HTML overlay for axis labels (escapes the SVG viewBox stretch).
  // Cleared first so prior-ticker labels don't bleed into the loading state.
  const labelsEl = modal.querySelector('.modal-chart-labels');
  if (labelsEl) labelsEl.innerHTML = '';
  const d = DATA[ticker];
  if (!d || !d.chart || !d.chart.points) {
    modalSvg.innerHTML = '';
    chartPoints = null;
    return;
  }
  const chart = d.chart;
  const rebased = d.rebased || d.prices.map(p => (p / d.baseline - 1) * 100);
  const dates = d.dates;
  const prices = d.prices;
  const n = chart.n;
  const xs = new Array(n);
  const ys = new Array(n);
  for (let i = 0; i < n; i++) {
    xs[i] = _modalX(i, n);
    ys[i] = _modalY(rebased[i], chart.vmin, chart.vmax);
  }
  const isUp = d.total >= 0;
  const color = isUp ? '#34d399' : '#f87171';
  const gradId = isUp ? 'grad-up-lg' : 'grad-down-lg';
  const labelY = MODAL_VB_PAD_T + MODAL_VB_INNER_H;
  // Area path = polyline + corner anchors. Cheap to rebuild from `chart.points`.
  const firstX = xs[0], lastX = xs[n - 1];
  const areaD = `M ${firstX.toFixed(1)},${labelY.toFixed(1)} L ${chart.points.replaceAll(' ', ' L ')} L ${lastX.toFixed(1)},${labelY.toFixed(1)} Z`;

  // Build SVG from precomputed geometry. v2.1 #1: text moved out of SVG into
  // HTML overlay (`labelsHtml`) so it renders at native browser DPI without
  // inheriting the SVG's non-uniform viewBox stretch. The SVG keeps grid
  // lines, the zero baseline, the polyline, gradients, and crosshair geometry.
  let html = '';
  const labelsHtml = [];
  // Y-axis: SVG line for the grid, HTML span for the percentage label.
  const yTickXPct = ((MODAL_VB_PAD_L - 4) / MODAL_VB_W * 100).toFixed(3);
  for (const t of chart.y_ticks) {
    html += `<line x1="${MODAL_VB_PAD_L}" y1="${t.y.toFixed(1)}" x2="${(MODAL_VB_PAD_L + MODAL_VB_INNER_W).toFixed(1)}" y2="${t.y.toFixed(1)}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>`;
    const yPct = (t.y / MODAL_VB_H * 100).toFixed(3);
    const sign = t.v >= 0 ? '+' : '';
    labelsHtml.push(`<span class="y-tick" style="left:${yTickXPct}%;top:${yPct}%">${sign}${t.v.toFixed(0)}%</span>`);
  }
  html += `<line x1="${MODAL_VB_PAD_L}" y1="${chart.zero_y.toFixed(1)}" x2="${(MODAL_VB_PAD_L + MODAL_VB_INNER_W).toFixed(1)}" y2="${chart.zero_y.toFixed(1)}" stroke="rgba(255,255,255,0.18)" stroke-width="0.8" stroke-dasharray="3 3"/>`;
  // X-axis: pure HTML, no SVG text. Position is just below the chart area
  // (labelY = chart's bottom edge, +12 viewBox-units of breathing room).
  const xLabelTopPct = ((labelY + 12) / MODAL_VB_H * 100).toFixed(3);
  for (const idx of chart.x_tick_idx) {
    const x = _modalX(idx, n);
    const xPct = (x / MODAL_VB_W * 100).toFixed(3);
    labelsHtml.push(`<span class="x-tick" style="left:${xPct}%;top:${xLabelTopPct}%">${fmtDate(dates[idx])}</span>`);
  }
  html += `<path d="${areaD}" fill="url(#${gradId})"/>`;
  // v1.9 #2: per-segment color on the modal polyline. Render each
  // same-sign run in its own color so below-baseline periods are visibly red
  // even when the position's overall total is positive. Falls back to the
  // single-color polyline if segments are missing (defensive for any payload
  // older than v1.9).
  if (Array.isArray(chart.segments) && chart.segments.length > 0) {
    for (const seg of chart.segments) {
      const segColor = seg.above ? '#34d399' : '#f87171';
      html += `<polyline points="${seg.pts}" fill="none" stroke="${segColor}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    }
  } else {
    html += `<polyline points="${chart.points}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  html += `<line class="crosshair" x1="0" y1="${MODAL_VB_PAD_T}" x2="0" y2="${labelY}" stroke="${color}" stroke-width="0.8" stroke-dasharray="2 3" opacity="0"/>`;
  html += `<circle class="dot" cx="0" cy="0" r="6" fill="${color}" stroke="${color}" opacity="0"/>`;

  // Transaction markers (buy/sell dots) — only if the per-stock transactions are available
  if (d.transactions && d.transactions.length > 0) {
    // De-dup: many rapid trades on a long axis snap to the same weekly bucket
    // and would stack into one illegible blob. Keep one marker per (week, side).
    const seenMarkers = new Set();
    for (const t of d.transactions) {
      const txnTime = new Date(t.date).getTime();
      let bestIdx = 0, bestDiff = Infinity;
      for (let i = 0; i < dates.length; i++) {
        const diff = Math.abs(new Date(dates[i]).getTime() - txnTime);
        if (diff < bestDiff) { bestDiff = diff; bestIdx = i; }
      }
      const seenKey = bestIdx + '|' + t.action;
      if (seenMarkers.has(seenKey)) continue;
      seenMarkers.add(seenKey);
      const mx = xs[bestIdx];
      const isBuy = t.action === 'BUY';
      const mColor = isBuy ? '#34d399' : '#f87171';
      const label = isBuy ? 'B' : 'S';
      const ly = labelY - 8;
      html += `<line x1="${mx.toFixed(1)}" y1="${MODAL_VB_PAD_T}" x2="${mx.toFixed(1)}" y2="${labelY.toFixed(1)}" stroke="${mColor}" stroke-width="0.7" stroke-dasharray="3 2" opacity="0.45"/>`;
      html += `<circle cx="${mx.toFixed(1)}" cy="${ly.toFixed(1)}" r="9" fill="${mColor}" stroke="#0b0e17" stroke-width="0.5"/>`;
      // v2.1 #1: the B/S character moves to the HTML overlay so it renders
      // legibly inside the circle. The circle itself stays in SVG and stretches
      // slightly into an ellipse, but at r=9 the distortion is barely visible.
      const mxPct = (mx / MODAL_VB_W * 100).toFixed(3);
      const lyPct = (ly / MODAL_VB_H * 100).toFixed(3);
      labelsHtml.push(`<span class="txn-marker" style="left:${mxPct}%;top:${lyPct}%">${label}</span>`);
    }
  }

  // Cost-basis tick: a faint amber vertical at the active-cycle baseline date,
  // marking where the headline % is anchored on the now-fuller path. Only drawn
  // when there is pre-baseline history (otherwise it just sits on the left edge).
  if (d.baseline_date && dates.length > 1) {
    const bTime = new Date(d.baseline_date).getTime();
    let bIdx = 0, bDiff = Infinity;
    for (let i = 0; i < dates.length; i++) {
      const diff = Math.abs(new Date(dates[i]).getTime() - bTime);
      if (diff < bDiff) { bDiff = diff; bIdx = i; }
    }
    if (bIdx > 0) {
      const cx = xs[bIdx];
      html += `<line x1="${cx.toFixed(1)}" y1="${MODAL_VB_PAD_T}" x2="${cx.toFixed(1)}" y2="${labelY.toFixed(1)}" stroke="#fbbf24" stroke-width="0.9" stroke-dasharray="2 3" opacity="0.55"/>`;
      const cxPct = (cx / MODAL_VB_W * 100).toFixed(3);
      const cyPct = ((MODAL_VB_PAD_T - 2) / MODAL_VB_H * 100).toFixed(3);
      labelsHtml.push(`<span class="cost-tick" style="left:${cxPct}%;top:${cyPct}%">cost</span>`);
    }
  }

  modalSvg.innerHTML = html;
  if (labelsEl) labelsEl.innerHTML = labelsHtml.join('');
  chartPoints = {xs, ys, dates, prices, rebased, color};
}

modalSvg.addEventListener('mousemove', (e) => {
  if (!chartPoints) return;
  const rect = modalSvg.getBoundingClientRect();
  const x = (e.clientX - rect.left) * (modalSvg.viewBox.baseVal.width / rect.width);
  let bestI = 0, bestDist = Infinity;
  for (let i = 0; i < chartPoints.xs.length; i++) {
    const d = Math.abs(chartPoints.xs[i] - x);
    if (d < bestDist) { bestDist = d; bestI = i; }
  }
  const px = chartPoints.xs[bestI], py = chartPoints.ys[bestI];
  const cross = modalSvg.querySelector('.crosshair');
  const dot = modalSvg.querySelector('.dot');
  cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('opacity', '0.7');
  dot.setAttribute('cx', px); dot.setAttribute('cy', py); dot.setAttribute('opacity', '1');
  const tipX = (px / modalSvg.viewBox.baseVal.width) * rect.width;
  const tipY = (py / modalSvg.viewBox.baseVal.height) * rect.height;
  const wrapRect = modalChartWrap.getBoundingClientRect();
  modalTip.style.left = (tipX + (rect.left - wrapRect.left)) + 'px';
  modalTip.style.top = (tipY + (rect.top - wrapRect.top)) + 'px';
  const reb = chartPoints.rebased[bestI];
  modalTip.innerHTML = `<div class="tip-date">${fmtDate(chartPoints.dates[bestI])}</div>` +
                       `<div><span class="tip-price">${fmtMoney(chartPoints.prices[bestI])}</span>` +
                       `<span class="tip-pct ${reb >= 0 ? 'pos' : 'neg'}">${fmtPct(reb, true)}</span></div>`;
  modalTip.removeAttribute('hidden');
});
modalSvg.addEventListener('mouseleave', () => {
  modalTip.setAttribute('hidden', '');
  const c = modalSvg.querySelector('.crosshair');
  const d = modalSvg.querySelector('.dot');
  if (c) c.setAttribute('opacity', '0');
  if (d) d.setAttribute('opacity', '0');
});

modal.querySelector('.modal-close').addEventListener('click', closeModal);
modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
// Stack-aware ESC: ticker modal takes priority (it's visually on top).
// If both modals are open, the FIRST Escape closes ticker only -- the info
// modal stays visible underneath. A second Escape closes info. This single
// handler replaces both the old ticker-only handler and the separate info
// handler in the click-to-expand block below.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!modal.hasAttribute('hidden')) { closeModal(); return; }
  if (typeof infoModal !== 'undefined' && infoModal && !infoModal.hasAttribute('hidden')) {
    closeInfoModal();
  }
});
// T1: resize handler removed — viewBox="0 0 1000 600" + preserveAspectRatio="none"
// makes the SVG scale natively. No re-render needed on window resize.

document.querySelectorAll('#ret-table tbody tr').forEach(row => {
  row.addEventListener('click', () => openModal(row.dataset.ticker));
});
document.querySelectorAll('.contrib-table tbody tr, .regret-table tbody tr, .dt-table tbody tr').forEach(row => {
  row.addEventListener('click', () => openModal(row.dataset.ticker));
});
document.querySelectorAll('.wl-card, .an-card').forEach(card => {
  card.addEventListener('click', () => openModal(card.dataset.ticker));
});
document.querySelectorAll('.io-stock').forEach(row => {
  row.addEventListener('click', () => openModal(row.dataset.ticker));
});
// v2.5 #9: Big Brain "couple" pages -> arrows flip between alternate sets.
document.querySelectorAll('.bigbrain-section[data-bb-pages]').forEach(sec => {
  const pages = Array.from(sec.querySelectorAll('.bb-page'));
  if (pages.length < 2) return;
  const ind = sec.querySelector('.bb-page-cur');
  let cur = 0;
  const show = i => {
    cur = (i + pages.length) % pages.length;
    pages.forEach((p, idx) => p.classList.toggle('active', idx === cur));
    if (ind) ind.textContent = (cur + 1);
  };
  const prev = sec.querySelector('.bb-prev');
  const next = sec.querySelector('.bb-next');
  if (prev) prev.addEventListener('click', () => show(cur - 1));
  if (next) next.addEventListener('click', () => show(cur + 1));
});
// v2.5 #3: Market expectations "Reshuffle" -> slide a window over the deeper
// theme pool, wrapping around (no repeats until the whole pool is exhausted).
document.querySelectorAll('.market-expectations-section').forEach(sec => {
  const list = sec.querySelector('.me-list');
  if (!list) return;
  const win = parseInt(list.dataset.meWindow || '0', 10);
  const rows = Array.from(list.querySelectorAll('.me-row'));
  if (!win || rows.length <= win) return;
  let start = 0;
  const paint = () => rows.forEach((r, i) => {
    r.style.display = (i >= start && i < start + win) ? '' : 'none';
  });
  paint();
  const btn = sec.querySelector('.me-reshuffle');
  if (btn) btn.addEventListener('click', () => {
    start += win;
    if (start >= rows.length) start = 0;
    paint();
  });
});
// v2.6 Value screen: page the scorecard rows in fixed windows with flip arrows.
document.querySelectorAll('.value-screen-section[data-vs-pages]').forEach(sec => {
  const table = sec.querySelector('.vs-table');
  const size = parseInt((table && table.dataset.vsPage) || '0', 10);
  const rows = Array.from(sec.querySelectorAll('.vs-row'));
  if (!size || rows.length <= size) return;
  const npages = Math.ceil(rows.length / size);
  const ind = sec.querySelector('.vs-page-cur');
  let cur = 0;
  const paint = () => {
    rows.forEach((r, i) => {
      r.style.display = (i >= cur * size && i < (cur + 1) * size) ? '' : 'none';
    });
    if (ind) ind.textContent = (cur + 1);
  };
  paint();
  const prev = sec.querySelector('.vs-prev');
  const next = sec.querySelector('.vs-next');
  if (prev) prev.addEventListener('click', () => { cur = (cur - 1 + npages) % npages; paint(); });
  if (next) next.addEventListener('click', () => { cur = (cur + 1) % npages; paint(); });
});
// v3.0 #4: watchlist arrow pager. Page size = however many cards fill the row
// (read live from the grid's column count), so page 1 fills before page 2 and
// it re-flows on resize. The nav hides itself when one row already holds all.
document.querySelectorAll('.watchlist-section[data-wl-pageable]').forEach(sec => {
  const grid = sec.querySelector('.wl-grid');
  const cards = Array.from(sec.querySelectorAll('.wl-card'));
  const nav = sec.querySelector('.wl-nav');
  const ind = sec.querySelector('.wl-page-cur');
  const tot = sec.querySelector('.wl-page-total');
  if (!grid || cards.length === 0) return;
  let cur = 0;
  const cols = () => {
    const tpl = getComputedStyle(grid).gridTemplateColumns;
    return Math.max(1, tpl.split(' ').filter(Boolean).length);
  };
  const npages = () => Math.max(1, Math.ceil(cards.length / cols()));
  const paint = () => {
    const size = cols(), np = npages();
    if (cur >= np) cur = np - 1;
    if (cur < 0) cur = 0;
    cards.forEach((c, i) => {
      c.style.display = (i >= cur * size && i < (cur + 1) * size) ? '' : 'none';
    });
    if (nav) nav.style.display = np > 1 ? '' : 'none';
    if (ind) ind.textContent = (cur + 1);
    if (tot) tot.textContent = np;
  };
  paint();
  const prev = sec.querySelector('.wl-prev');
  const next = sec.querySelector('.wl-next');
  if (prev) prev.addEventListener('click', () => { const np = npages(); cur = (cur - 1 + np) % np; paint(); });
  if (next) next.addEventListener('click', () => { const np = npages(); cur = (cur + 1) % np; paint(); });
  let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(paint, 150); });
});
// T9/T10: generic ticker-clickable handler. Anything bearing the class +
// data-ticker opens the modal. Used by the rating-moves panel ticker spans
// and the unusual-volume hero chips. Cheaper than registering per-section
// selectors and stays correct if future sections use the same convention.
document.querySelectorAll('.ticker-clickable[data-ticker]').forEach(el => {
  el.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const t = el.dataset.ticker;
    // v3.4 #5: non-owned names (no DATA payload) but with an analyst snapshot
    // open a lightweight card instead of the (chartless) full modal no-op.
    if (!DATA[t] && typeof RM_ANALYST !== 'undefined' && RM_ANALYST[t]) { openAnalystInfo(t); return; }
    openModal(t);
  });
});

// v3.4 #5: render the lightweight analyst card in the info-modal shell.
function openAnalystInfo(t) {
  const d = (typeof RM_ANALYST !== 'undefined') ? RM_ANALYST[t] : null;
  if (!d) return;
  openInfoModal(d.title, d.sub, d.html);
}

// v3.4 #1: Doctor "see <module>" links. A hidden module is display:none, so
// reveal it first (re-using the module-visibility change handler to persist),
// then smooth-scroll and briefly flash it.
document.querySelectorAll('.doc-link[data-module-target]').forEach(el => {
  el.addEventListener('click', (e) => {
    e.preventDefault();
    const id = el.dataset.moduleTarget;
    const mod = document.querySelector('#module-stack > .module[data-module="' + id + '"]');
    if (!mod) return;
    if (mod.dataset.hidden === 'true') {
      const cb = mod.querySelector('.module-vis-cb');
      if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
      else { mod.dataset.hidden = 'false'; }
    }
    mod.scrollIntoView({ behavior: 'smooth', block: 'start' });
    mod.classList.remove('module-flash');
    void mod.offsetWidth;               // restart the flash animation if re-clicked
    mod.classList.add('module-flash');
    setTimeout(() => mod.classList.remove('module-flash'), 1500);
  });
});

// ============================================================================
// T11/T12/T14/T15: info-modal stack for click-to-expand drill-downs.
//
// Architecture: separate modal element (#info-modal) at lower z-index than
// the existing ticker modal. When a user clicks an industry / sector /
// pair-bucket / weekly-point, openInfoModal() shows the info-modal with the
// relevant content. Tickers within the info-modal also have .ticker-clickable
// -- their handler opens the existing ticker modal ON TOP of the info-modal
// (true stacking; closing the ticker modal returns to the info-modal).
//
// ESC key + backdrop click are routed via stack priority: if the ticker
// modal is open, those gestures close it first; only when it's already
// closed do they affect the info-modal.
// ============================================================================
const infoModal = document.getElementById('info-modal');
const infoModalTitle = infoModal.querySelector('.info-modal-title');
const infoModalSub = infoModal.querySelector('.info-modal-sub');
const infoModalBody = infoModal.querySelector('.info-modal-body');
const infoModalClose = document.getElementById('info-modal-close');

function openInfoModal(title, sub, contentHtml) {
  infoModalTitle.innerHTML = title || '';
  infoModalSub.innerHTML = sub || '';
  infoModalBody.innerHTML = contentHtml || '';
  infoModal.removeAttribute('hidden');
  document.body.classList.add('modal-open');
  // v1.8.1 B5: reset scroll to top on new content so switching from one
  // drill-down to another always starts at the title, not mid-list.
  const ic = infoModal.querySelector('.modal-card');
  if (ic) ic.scrollTop = 0;
  // Wire any ticker-clickable spans inside the new content -- they were
  // injected after the initial DOMContentLoaded handlers ran, so attach now.
  // Use _safeOpenTicker so universe-only tickers get a sensible fallback
  // instead of silently failing.
  infoModalBody.querySelectorAll('.ticker-clickable[data-ticker]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      _safeOpenTicker(el.dataset.ticker);
    });
  });
}
function closeInfoModal() {
  infoModal.setAttribute('hidden', '');
  // Only release modal-open if the ticker modal isn't itself still open.
  if (modal.hasAttribute('hidden')) document.body.classList.remove('modal-open');
}

infoModalClose.addEventListener('click', closeInfoModal);
infoModal.addEventListener('click', (e) => { if (e.target === infoModal) closeInfoModal(); });
// ESC handling is centralized in the stack-aware handler near closeModal()
// above -- the consolidated handler closes the topmost open modal first.

// ----- Helpers for assembling info-modal content -----
function _capTierBadge(tier) {
  if (!tier) return '';
  return `<span class="im-tier im-tier-${tier.toLowerCase()}">${tier}</span>`;
}
function _pctSpan(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return '<span class="muted">&mdash;</span>';
  const cls = v >= 0 ? 'pos' : 'neg';
  return `<span class="${cls}">${v >= 0 ? '+' : ''}${v.toFixed(1)}%</span>`;
}
// Universe-aware ticker opener. If DATA has the ticker (held / watch-listed),
// opens the existing rich ticker modal. Otherwise shows a "universe only"
// fallback info-modal with the limited fields we have for that ticker
// (industry, name, 12mo return, cap tier). This prevents silent no-ops when
// users click on universe-only tickers in the industry outlook breakdown.
function _safeOpenTicker(ticker) {
  if (DATA[ticker]) { openModal(ticker); return; }
  let entry = null, industry = '';
  const inds = AUX_DATA.industries || {};
  for (const ind in inds) {
    const m = inds[ind].find(e => e.ticker === ticker);
    if (m) { entry = m; industry = ind; break; }
  }
  if (entry) {
    const tierBadge = entry.cap_tier ? ' ' + _capTierBadge(entry.cap_tier) : '';
    const body = (
      `<table class="im-table im-uni-table"><tbody>`
      + `<tr><td class="muted">Name</td><td>${escapeNewsHtml(entry.name)}${tierBadge}</td></tr>`
      + `<tr><td class="muted">Industry</td><td>${escapeNewsHtml(industry)}</td></tr>`
      + `<tr><td class="muted">12-month return</td><td class="num">${_pctSpan(entry.return_12mo)}</td></tr>`
      + `</tbody></table>`
      + `<p class="muted im-uni-note">Limited data: this ticker is in the reference `
      + `<code>universe.csv</code> but not in your basket or watchlist. Add it to `
      + `<code>log.xlsx</code> or <code>watchlist.csv</code> for full chart, modal stats, and news.</p>`
    );
    openInfoModal(`${escapeNewsHtml(ticker)} &middot; universe only`, escapeNewsHtml(industry), body);
  } else {
    openInfoModal(escapeNewsHtml(ticker), 'no detail data',
      `<p class="muted">No detail data is loaded for this ticker.</p>`);
  }
}

// T11: industry-card click -> info-modal listing every ticker in that industry.
document.querySelectorAll('.industry-clickable[data-industry]').forEach(card => {
  card.addEventListener('click', (e) => {
    // If the user clicked an inner stock row whose ticker has detail DATA
    // (i.e. it's a held / watchlisted name with full OHLCV), let the inner
    // handler's openModal() win -- don't ALSO open the industry modal.
    // But if it's a universe-only ticker (no DATA), the inner openModal call
    // would silently fail; fall through to open the industry overview info
    // modal as the next-best drill-down.
    const inner = e.target.closest && e.target.closest('.io-stock');
    if (inner && inner.dataset.ticker && DATA[inner.dataset.ticker]) return;
    const ind = card.dataset.industry;
    const entries = (AUX_DATA.industries && AUX_DATA.industries[ind]) || [];
    const rows = entries.map(en => (
      `<tr class="ticker-clickable" data-ticker="${en.ticker}">`
      + `<td class="im-tkr">${en.ticker}${_capTierBadge(en.cap_tier)}</td>`
      + `<td class="im-name">${escapeNewsHtml(en.name)}</td>`
      + `<td class="num im-ret">${_pctSpan(en.return_12mo)}</td>`
      + `</tr>`
    )).join('');
    const sub = `${entries.length} tracked ticker${entries.length === 1 ? '' : 's'} &middot; sorted by 12-mo return`;
    const body = entries.length
      ? `<table class="im-table"><thead><tr><th>Ticker</th><th>Name</th><th class="num">12-mo</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="muted">No tracked tickers in this industry.</p>`;
    openInfoModal(`Industry &middot; ${escapeNewsHtml(ind)}`, sub, body);
  });
});

// T12: attribution-row click -> info-modal listing every open position in
// that industry (matched by the same industry-or-sector-fallback key).
document.querySelectorAll('.attribution-row-clickable[data-industry]').forEach(row => {
  row.addEventListener('click', () => {
    const key = row.dataset.industry;
    const positions = (AUX_DATA.sectors && AUX_DATA.sectors[key]) || [];
    const rows = positions.map(p => (
      `<tr class="ticker-clickable" data-ticker="${p.ticker}">`
      + `<td class="im-tkr">${p.ticker}</td>`
      + `<td class="im-name">${escapeNewsHtml(p.name)}</td>`
      + `<td class="num">${p.weight_pct.toFixed(1)}%</td>`
      + `<td class="num">${_pctSpan(p.total_pct)}</td>`
      + `<td class="num">${_pctSpan(p.contribution_pp)}</td>`
      + `</tr>`
    )).join('');
    const sub = `${positions.length} open position${positions.length === 1 ? '' : 's'} &middot; sorted by contribution`;
    const body = positions.length
      ? `<table class="im-table"><thead><tr><th>Ticker</th><th>Name</th><th class="num">Weight</th><th class="num">Return</th><th class="num">Contrib</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="muted">No open positions found for this industry.</p>`;
    openInfoModal(`Industry attribution &middot; ${key}`, sub, body);
  });
});

// T14: histogram-column click -> info-modal listing every pair whose
// correlation falls within the clicked bucket. The pairs list comes from
// AUX_DATA.pairs (pre-sorted by abs(corr) desc); we filter to [lo, hi]
// inline. Click target is the full-height column wrapper, so even tiny
// bars are easy to hit.
document.querySelectorAll('.div-hist-col-clickable[data-bucket-lo]').forEach(bar => {
  bar.addEventListener('click', () => {
    const lo = parseFloat(bar.dataset.bucketLo);
    const hi = parseFloat(bar.dataset.bucketHi);
    const pairs = (AUX_DATA.pairs || []).filter(p => p.corr >= lo && p.corr <= hi);
    const rows = pairs.map(p => {
      const cls = p.corr >= 0.6 ? 'neg' : (p.corr <= 0 ? 'pos' : '');
      return (
        `<tr>`
        + `<td><span class="ticker-clickable im-tkr" data-ticker="${p.a}">${p.a}</span></td>`
        + `<td><span class="im-arrow">&harr;</span></td>`
        + `<td><span class="ticker-clickable im-tkr" data-ticker="${p.b}">${p.b}</span></td>`
        + `<td class="num ${cls}">${p.corr >= 0 ? '+' : ''}${p.corr.toFixed(2)}</td>`
        + `</tr>`
      );
    }).join('');
    const sub = `${pairs.length} pair${pairs.length === 1 ? '' : 's'} in range &middot; click any ticker for detail`;
    const body = pairs.length
      ? `<table class="im-table"><thead><tr><th>A</th><th></th><th>B</th><th class="num">&rho;</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="muted">No pairs found in this bucket.</p>`;
    openInfoModal(`Correlation bucket &middot; ${lo.toFixed(2)} to ${hi.toFixed(2)}`, sub, body);
  });
});

// T15: hero-week-click rect -> info-modal of that week's top/bottom movers
// (basket-wide, in base currency). Delegated off the hero SVG since the
// rects are inserted dynamically by renderHeroChart (which fires multiple
// times across responsive resizes).
document.getElementById('hero-chart').addEventListener('click', (e) => {
  const rect = e.target.closest && e.target.closest('.hero-week-click');
  if (!rect) return;
  const dateKey = rect.dataset.weekEnd;
  const movers = AUX_DATA.weekly_movers || {};
  let wkKey = dateKey;
  let wk = movers[wkKey];
  if (!wk) {
    // v3.5: the 3M/1M views plot DAILY points but movers are keyed by
    // week-ending date, so an exact lookup always missed there. Map the
    // clicked day to its containing week: the nearest key on/after the
    // day, within 7 days (the newest partial week has no key yet and
    // falls through to the empty-state).
    const d0 = Date.parse(dateKey);
    let best = Infinity;
    for (const k of Object.keys(movers)) {
      const dk = Date.parse(k);
      if (dk >= d0 && dk - d0 < 7 * 86400000 && dk < best) { best = dk; wkKey = k; }
    }
    if (best !== Infinity) wk = movers[wkKey];
  }
  if (!wk) {
    openInfoModal(`Week ending ${dateKey}`,
      'no movers recorded for this week',
      '<p class="muted">No movement data available for this week.</p>');
    return;
  }
  const mkRow = (m) => (
    `<tr class="ticker-clickable" data-ticker="${m.ticker}">`
    + `<td class="im-tkr">${m.ticker}</td>`
    + `<td class="num">${_pctSpan(m.pct)}</td>`
    + `</tr>`
  );
  const up   = (wk.up   || []).map(mkRow).join('');
  const down = (wk.down || []).map(mkRow).join('');
  const body = (
    `<div class="im-movers">`
    + `<div class="im-movers-col">`
    +   `<h4 class="im-movers-h pos">Top movers up</h4>`
    +   (up   ? `<table class="im-table"><tbody>${up}</tbody></table>`   : `<p class="muted">none</p>`)
    + `</div>`
    + `<div class="im-movers-col">`
    +   `<h4 class="im-movers-h neg">Top movers down</h4>`
    +   (down ? `<table class="im-table"><tbody>${down}</tbody></table>` : `<p class="muted">none</p>`)
    + `</div>`
    + `</div>`
  );
  openInfoModal(`Week ending ${wkKey}`,
    'top movers across your held tickers',
    body);
});

// ---- Palette toggle ---------------------------------------------------
// Body class controls which set of CSS variables wins. Persist the choice
// across visits via localStorage so the page remembers the user's preference.
// Desktop-view override: lets users on narrow viewports force the full
// desktop layout (page becomes horizontally scrollable). Persisted via
// localStorage so the choice survives reloads. Mirrors the palette toggle's
// state-management pattern.
(function setupDesktopMode() {
  const btn = document.getElementById('desktop-mode-btn');
  if (!btn) return;
  const KEY = 'stocks-dashboard-force-desktop';
  function apply(forced) {
    document.body.classList.toggle('force-desktop', forced);
    btn.classList.toggle('active', forced);
    btn.setAttribute('aria-pressed', forced ? 'true' : 'false');
    btn.textContent = forced ? 'Mobile view' : 'Desktop view';
  }
  let saved = false;
  try { saved = localStorage.getItem(KEY) === '1'; } catch (e) {}
  apply(saved);
  btn.addEventListener('click', () => {
    const next = !document.body.classList.contains('force-desktop');
    apply(next);
    try { localStorage.setItem(KEY, next ? '1' : '0'); } catch (e) {}
  });
})();

// v1.9 Pocket Lesson: random tip on load + Next-tip rotation + topbar toggle.
// State: `pocketLessonOn` in localStorage ('1' / '0'). Default is OFF -- the
// card is collapsed until the user opens it from the topbar button. Once
// they toggle, the choice persists.
// The card transitions in/out via the .is-open class (max-height + margin +
// opacity), so the rest of the page smoothly slides to make/yield space.
(function setupPocketLesson() {
  const STORAGE_KEY = 'pocketLessonOn';
  const CAT_KEY = 'pocketLessonCategory';
  const btn = document.getElementById('pocket-lesson-btn');
  const wrap = document.getElementById('pocket-lesson-wrap');
  const titleEl = document.getElementById('pocket-lesson-title');
  const bodyEl = document.getElementById('pocket-lesson-body');
  const counterEl = document.getElementById('pocket-lesson-counter');
  const nextBtn = document.getElementById('pocket-lesson-next');
  const catPillEl = document.getElementById('pocket-lesson-cat-pill');
  const filtersEl = document.getElementById('pocket-lesson-filters');
  if (!btn || !wrap || !Array.isArray(POCKET_LESSONS) || POCKET_LESSONS.length === 0) return;

  // Active category filter ('*' means all). Persisted so the user's choice
  // survives reloads alongside the visibility state.
  let activeCategory = '*';
  try { activeCategory = localStorage.getItem(CAT_KEY) || '*'; } catch (e) {}
  // Track currentIdx so Next can avoid showing the same tip twice in a row.
  let currentIdx = -1;

  function eligibleIndices() {
    if (activeCategory === '*') return POCKET_LESSONS.map((_, i) => i);
    const out = [];
    for (let i = 0; i < POCKET_LESSONS.length; i++) {
      if (POCKET_LESSONS[i].category === activeCategory) out.push(i);
    }
    return out.length ? out : POCKET_LESSONS.map((_, i) => i);   // fallback: all
  }
  function pickRandomTip() {
    const pool = eligibleIndices();
    if (pool.length === 1) return pool[0];
    let idx;
    do { idx = pool[Math.floor(Math.random() * pool.length)]; }
    while (idx === currentIdx);
    return idx;
  }
  function renderTip(idx) {
    const tip = POCKET_LESSONS[idx];
    if (!tip) return;
    currentIdx = idx;
    titleEl.textContent = tip.title || '';
    bodyEl.textContent = tip.body || '';
    catPillEl.textContent = tip.category || '';
    const pool = eligibleIndices();
    const posInPool = pool.indexOf(idx) + 1;
    counterEl.textContent = activeCategory === '*'
      ? `Tip ${idx + 1} of ${POCKET_LESSONS.length}`
      : `${posInPool} of ${pool.length} in ${activeCategory}`;
  }

  // Build the filter chips dynamically from the categories actually present.
  function buildChips() {
    const cats = new Set();
    for (const l of POCKET_LESSONS) if (l.category) cats.add(l.category);
    // Order: All first, then the rest alphabetically -- stable across rebuilds.
    const ordered = ['*'].concat(Array.from(cats).sort());
    filtersEl.innerHTML = ordered.map(c => {
      const label = c === '*' ? `All ${POCKET_LESSONS.length}` : c;
      const cls = c === activeCategory ? 'pocket-lesson-chip active' : 'pocket-lesson-chip';
      return `<button type="button" class="${cls}" data-cat="${c}">${label}</button>`;
    }).join('');
    filtersEl.querySelectorAll('.pocket-lesson-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        activeCategory = chip.dataset.cat;
        try { localStorage.setItem(CAT_KEY, activeCategory); } catch (e) {}
        filtersEl.querySelectorAll('.pocket-lesson-chip').forEach(c =>
          c.classList.toggle('active', c.dataset.cat === activeCategory));
        renderTip(pickRandomTip());
      });
    });
  }
  buildChips();

  function setEnabled(on, opts) {
    opts = opts || {};
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    wrap.classList.toggle('is-open', on);
    wrap.setAttribute('aria-hidden', on ? 'false' : 'true');
    if (on && currentIdx < 0) renderTip(pickRandomTip());
    if (!opts.silent) {
      try { localStorage.setItem(STORAGE_KEY, on ? '1' : '0'); } catch (e) {}
    }
  }

  let initial = false;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === '1') initial = true;
  } catch (e) {}
  setEnabled(initial, {silent: true});

  btn.addEventListener('click', () => setEnabled(btn.getAttribute('aria-pressed') !== 'true'));
  nextBtn.addEventListener('click', () => renderTip(pickRandomTip()));
})();

// v3.2 Doctor: toggled check-up panel. State `doctorOn` in localStorage
// ('1'/'0'), default OFF. Mirrors the pocket-lesson slide-open pattern.
(function setupDoctor() {
  const STORAGE_KEY = 'doctorOn';
  const btn = document.getElementById('doctor-btn');
  const wrap = document.getElementById('doctor-wrap');
  if (!btn || !wrap) return;
  function setEnabled(on, opts) {
    opts = opts || {};
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    wrap.classList.toggle('is-open', on);
    wrap.setAttribute('aria-hidden', on ? 'false' : 'true');
    if (!opts.silent) {
      try { localStorage.setItem(STORAGE_KEY, on ? '1' : '0'); } catch (e) {}
    }
  }
  let initial = false;
  try { initial = localStorage.getItem(STORAGE_KEY) === '1'; } catch (e) {}
  setEnabled(initial, {silent: true});
  btn.addEventListener('click', () => setEnabled(btn.getAttribute('aria-pressed') !== 'true'));
})();

// v3.3 Shockwave: interactive market stress test over the baked SHOCKWAVE const.
// Toggles like the Doctor; panel builds lazily on first open.
(function setupShockwave() {
  var btn=document.getElementById('shockwave-btn'), wrap=document.getElementById('shockwave-wrap');
  if(!btn||!wrap||typeof SHOCKWAVE==='undefined') return;
  var KEY='shockwaveOn', SKEY='shockwaveSize';
  function setOpen(on,silent){
    btn.setAttribute('aria-pressed',on?'true':'false');
    wrap.classList.toggle('is-open',on);
    wrap.setAttribute('aria-hidden',on?'false':'true');
    if(on && !wrap.dataset.init){ initPanel(); wrap.dataset.init='1'; }
    if(!silent){ try{localStorage.setItem(KEY,on?'1':'0');}catch(e){} }
  }
  var i0=false; try{i0=localStorage.getItem(KEY)==='1';}catch(e){}
  setOpen(i0,true);
  btn.addEventListener('click',function(){setOpen(btn.getAttribute('aria-pressed')!=='true');});

  function initPanel(){
    var F=SHOCKWAVE.factors||[], P=SHOCKWAVE.presets||[], base=SHOCKWAVE.base_ccy||'GBP';
    var LOSS='#f87171', GAIN='#34d399', FLAT='#8a8a8a';
    var LK={common:'#34d399',occasional:'#fbbf24',rare:'#f87171'};
    var sizeMode=SHOCKWAVE.size_default||'eq'; try{var sm=localStorage.getItem(SKEY); if(sm)sizeMode=sm;}catch(e){}
    var usd=0, active=null, showAll=false;
    var maxMc=Math.max.apply(null,F.map(function(f){return f.mcap||0;}).concat([1]));
    var chips=document.getElementById('sw-chips'), sliders=document.getElementById('sw-sliders'),
        sizeBox=document.getElementById('sw-size'), hero=document.getElementById('sw-hero'),
        action=document.getElementById('sw-action'), field=document.getElementById('sw-field'),
        impact=document.getElementById('sw-impact');
    sliders.innerHTML=''
      +'<div class="sw-srow"><span class="l">Market (SPY)</span><input id="sw-mkt" type="range" min="-45" max="20" step="1" value="-20"><span class="v" id="sw-mktv"></span></div>'
      +'<div class="sw-srow"><span class="l">Tech tilt</span><input id="sw-tec" type="range" min="-30" max="30" step="1" value="-10"><span class="v" id="sw-tecv"></span></div>'
      +'<div class="sw-srow"><span class="l">USD vs '+base+'</span><input id="sw-usd" type="range" min="-15" max="15" step="1" value="0"><span class="v" id="sw-usdv"></span></div>';
    var mkt=document.getElementById('sw-mkt'), tec=document.getElementById('sw-tec'), usdS=document.getElementById('sw-usd');
    [['eq','Equal'],['w','By weight'],['mc','By market cap']].forEach(function(m){
      var b=document.createElement('button'); b.className='sw-seg'+(m[0]===sizeMode?' on':''); b.textContent=m[1];
      b.onclick=function(){sizeMode=m[0]; try{localStorage.setItem(SKEY,m[0]);}catch(e){} [].forEach.call(sizeBox.children,function(c){c.className='sw-seg';}); b.className='sw-seg on'; render();};
      sizeBox.appendChild(b);
    });
    P.forEach(function(p){
      var b=document.createElement('button'); b.className='sw-chip';
      b.innerHTML='<span class="sw-lk" style="background:'+(LK[p.likelihood]||FLAT)+'"></span>'+p.label+(p.recovery?'<span style="color:var(--text-dim)"> '+p.recovery+'</span>':'');
      b.onclick=function(){mkt.value=p.spy; tec.value=p.tech; usd=p.usd; usdS.value=p.usd; active=p; render();};
      chips.appendChild(b);
    });
    var X=function(b){return 66+(Math.max(0.2,Math.min(2.4,b))-0.2)/2.2*796;};
    var Y=function(r){return 300-(Math.max(-100,Math.min(80,r))+100)/180*270;};
    function col(m){return m<-3?LOSS:m>3?GAIN:FLAT;}
    function fmt(x){return (x>0?'+':'')+Math.round(x)+'%';}
    function rad(f){if(sizeMode==='eq')return 5; if(sizeMode==='w'){var w=f.weight||1; return 3+Math.min(6,w*2.1);} if(!f.mcap)return 5; return 3+Math.sqrt(f.mcap/maxMc)*7;}
    function move(f,sc){var bm=(f.b_mkt===f.b_mkt&&f.b_mkt!=null)?f.b_mkt:0, bt=(f.b_tech===f.b_tech&&f.b_tech!=null)?f.b_tech:0; var m=bm*sc.spy+bt*sc.tech; if((f.ccy||base)===SHOCKWAVE.fx_ccy && SHOCKWAVE.fx_ccy!==base) m+=sc.usd; return Math.max(m,-100);}
    function recov(b){if(b>=-6)return null; if(b>=-15)return'~1 year'; if(b>=-25)return'~2 years'; if(b>=-40)return'~4 years'; return'~5-6 years';}
    function render(){
      var sc={spy:+mkt.value, tech:+tec.value, usd:usd};
      document.getElementById('sw-mktv').textContent=fmt(+mkt.value);
      document.getElementById('sw-tecv').textContent=fmt(+tec.value);
      document.getElementById('sw-usdv').textContent=fmt(usd);
      var d=F.map(function(f){var m=move(f,sc); return {f:f,t:f.ticker,m:m,proj:Math.max((f.ret||0)+m,-100),w:f.weight||1,bm:(f.b_mkt||0),lc:f.low_conf,contrib:Math.abs(m)*(f.weight||1)};});
      var tw=d.reduce(function(a,x){return a+x.w;},0)||1;
      var basket=d.reduce(function(a,x){return a+x.w*x.m;},0)/tw;
      var rc=active&&active.recovery?active.recovery:recov(basket);
      hero.innerHTML=fmt(basket)+'<small>estimated basket move'+(rc?' &middot; est. recovery '+rc:'')+'</small>';
      hero.style.color=col(basket);
      var worst=d.slice().sort(function(a,b){return a.m-b.m;})[0]||{t:'',m:0};
      var best=d.slice().sort(function(a,b){return b.m-a.m;})[0]||{t:'',m:0};
      var act;
      if(basket<=-15) act='Deep drawdown. Most exposed: '+worst.t+' ('+fmt(worst.m)+')'+(rc?', ~'+rc.replace(/[~ ]/g,'')+' to recover historically':'')+' &mdash; consider trimming high-beta names.';
      else if(basket<=-5) act='Moderate hit. '+worst.t+' leads the downside ('+fmt(worst.m)+') &mdash; worth a hedge or a lighter position.';
      else if(basket<3) act='Your basket looks resilient to this scenario &mdash; no urgent action.';
      else act='Upside scenario: '+best.t+' benefits most ('+fmt(best.m)+'). Defensive names lag.';
      action.innerHTML=act;
      var labeled={};
      d.slice().sort(function(a,b){return b.contrib-a.contrib;}).slice(0,SHOCKWAVE_LABEL_TOP).forEach(function(x){labeled[x.t]=1;});
      d.forEach(function(x){ if(x.bm>1.2 && x.proj<0 && x.m<-30) labeled[x.t]=1; });
      var placed=[];
      function labelY(cx,cy,r){
        var up=cy-r-4, dn=cy+r+12;
        function clash(y){ return placed.some(function(p){ return Math.abs(p.x-cx)<34 && Math.abs(p.y-y)<11; }); }
        var y = !clash(up) ? up : (!clash(dn) ? dn : up);
        placed.push({x:cx,y:y}); return y;
      }
      field.innerHTML=''
        +'<rect x="'+X(1.2).toFixed(1)+'" y="'+Y(0).toFixed(1)+'" width="'+(862-X(1.2)).toFixed(1)+'" height="'+(300-Y(0)).toFixed(1)+'" fill="'+LOSS+'" opacity="0.08"></rect>'
        +'<line x1="66" y1="300" x2="862" y2="300" stroke="var(--border)"></line>'
        +'<line x1="66" y1="30" x2="66" y2="300" stroke="var(--border)"></line>'
        +'<line x1="66" y1="'+Y(0).toFixed(1)+'" x2="862" y2="'+Y(0).toFixed(1)+'" stroke="var(--border)" stroke-dasharray="3 3"></line>'
        +'<text x="464" y="334" text-anchor="middle" font-size="11" fill="var(--text-dim)">market sensitivity &rarr;</text>'
        +'<text x="18" y="165" text-anchor="middle" font-size="11" fill="var(--text-dim)" transform="rotate(-90 18 165)">projected return</text>'
        +'<text x="58" y="34" text-anchor="end" font-size="10.5" fill="var(--text-dim)">+80%</text>'
        +'<text x="58" y="'+(Y(0)+4).toFixed(1)+'" text-anchor="end" font-size="10.5" fill="var(--text-dim)">0%</text>'
        +'<text x="58" y="'+(Y(-50)+4).toFixed(1)+'" text-anchor="end" font-size="10.5" fill="var(--text-dim)">-50%</text>'
        +'<text x="58" y="302" text-anchor="end" font-size="10.5" fill="var(--text-dim)">-100%</text>'
        +d.map(function(x){
          var cx=X(x.bm), cy=Y(x.proj), r=rad(x.f), c=col(x.m), lab=labeled[x.t], op=x.lc?0.3:(lab?1:0.5),
              pulse=(x.m<SHOCKWAVE_PULSE_PCT && x.bm>1.2)?' pz':'';
          var tx='';
          if(lab){ var ly=labelY(cx,cy,r); tx='<text x="'+cx.toFixed(1)+'" y="'+ly.toFixed(1)+'" text-anchor="middle" font-size="10" fill="var(--text-dim)">'+x.t+'</text>'; }
          return '<g class="dot" style="transform:translate('+cx.toFixed(1)+'px,'+cy.toFixed(1)+'px)"><circle class="'+pulse.trim()+'" r="'+r.toFixed(1)+'" fill="'+c+'" opacity="'+op+'"><title>'+x.t+': '+fmt(x.m)+(x.lc?' (low fit)':'')+'</title></circle></g>'+tx;
        }).join('');
      var ranked=d.slice().sort(function(a,b){return b.contrib-a.contrib;});
      var shown=showAll?ranked:ranked.slice(0,6);
      var mx=Math.max.apply(null,ranked.map(function(x){return Math.abs(x.m);}).concat([1]));
      var ihead='<div class="sw-ihead"><span style="width:56px">name</span><span style="flex:1"></span>'
        +'<span class="sw-ibrk">mkt / tech / FX</span><span style="width:46px;text-align:right">net</span></div>';
      impact.innerHTML=ihead+shown.map(function(x){
        var neg=x.m<0, w=(Math.abs(x.m)/mx)*46, c=col(x.m);
        var fxApplies=((x.f.ccy||base)===SHOCKWAVE.fx_ccy && SHOCKWAVE.fx_ccy!==base);
        var mktC=(x.f.b_mkt||0)*sc.spy, techC=(x.f.b_tech||0)*sc.tech, fxC=fxApplies?sc.usd:0;
        var rawSum=mktC+techC+fxC;
        var brk='market '+fmt(mktC)+' + tech '+fmt(techC)+(fxApplies?' + FX '+fmt(fxC):'')+' = '+fmt(rawSum)+(rawSum<-100?' (capped at -100%)':'');
        return '<div class="sw-ibar" title="'+brk+'"><span style="width:56px;font-weight:600">'+x.t+'</span>'
          +'<div style="position:relative;flex:1;height:16px"><div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--border)"></div>'
          +'<div style="position:absolute;top:2px;height:12px;border-radius:3px;background:'+c+';'+(neg?'right:50%;':'left:50%;')+'width:'+w+'%"></div></div>'
          +'<span class="sw-ibrk">'+fmt(mktC)+' / '+fmt(techC)+' / '+(fxApplies?fmt(fxC):'&mdash;')+'</span>'
          +'<span style="width:46px;text-align:right;font-weight:600;color:'+c+'">'+fmt(x.m)+'</span></div>';
      }).join('')+(ranked.length>6?'<div class="sw-more" id="sw-more">'+(showAll?'Show less':'Show all '+ranked.length)+'</div>':'');
      var more=document.getElementById('sw-more');
      if(more) more.onclick=function(){showAll=!showAll; render();};
    }
    mkt.addEventListener('input',function(){active=null; render();});
    tec.addEventListener('input',function(){active=null; render();});
    usdS.addEventListener('input',function(){usd=+usdS.value; active=null; render();});
    render();
  }
})();

// v3.4 #2 Signal stacking: toggled recurrence panel over the baked history.
(function setupSignalStacking() {
  const STORAGE_KEY = 'signalStackingOn';
  const btn = document.getElementById('signal-stacking-btn');
  const wrap = document.getElementById('signal-stacking-wrap');
  if (!btn || !wrap) return;
  function setEnabled(on, opts) {
    opts = opts || {};
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    wrap.classList.toggle('is-open', on);
    wrap.setAttribute('aria-hidden', on ? 'false' : 'true');
    if (!opts.silent) {
      try { localStorage.setItem(STORAGE_KEY, on ? '1' : '0'); } catch (e) {}
    }
  }
  let initial = false;
  try { initial = localStorage.getItem(STORAGE_KEY) === '1'; } catch (e) {}
  setEnabled(initial, {silent: true});
  btn.addEventListener('click', () => setEnabled(btn.getAttribute('aria-pressed') !== 'true'));
})();

// v2.1: palette toggle collapsed from 4 buttons into 1 cycling button. Each
// click advances through the ORDER list; label updates to the current palette
// name. Persistence key + body-class scheme unchanged from v1.x for backwards
// compat -- localStorage values "default" / "softdark" / "light" / "bloomberg"
// still apply correctly.
(function setupPalette() {
  const PALETTE_KEY = 'stocks-dashboard-palette';
  const ORDER = ['default', 'softdark', 'light', 'bloomberg'];
  const LABELS = {default: 'Default', softdark: 'Soft Dark', light: 'Light', bloomberg: 'Amber'};
  const btn = document.getElementById('palette-cycle-btn');
  if (!btn) return;
  function apply(name) {
    document.body.classList.remove('palette-softdark','palette-light','palette-bloomberg');
    if (name && name !== 'default') document.body.classList.add('palette-' + name);
    // v2.1: button is icon-only; the human-readable name now lives in the
    // data-tooltip attribute (which drives the CSS hover tooltip).
    btn.dataset.tooltip = LABELS[name] || 'Default';
    btn.dataset.palette = name;
    try { localStorage.setItem(PALETTE_KEY, name); } catch (e) { /* private mode */ }
  }
  const saved = (() => { try { return localStorage.getItem(PALETTE_KEY); } catch (e) { return null; } })();
  apply(ORDER.includes(saved) ? saved : 'default');
  btn.addEventListener('click', () => {
    const current = btn.dataset.palette || 'default';
    const next = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
    apply(next);
  });
})();

// v2.1 Quiz feature. Opens via topbar Quiz button -> presents one question
// at a time from QUIZ_POOL, with a 3-option choice. Picks the next question
// uniformly at random from the "unseen" pool; recycles the pool when 90%+
// has been seen. Monthly score (answered + correct) resets on each new
// calendar month, tracked via the "month" key in quizMonthly.
//
// localStorage schema:
//   "quizSeen"    : JSON array of seen question ids
//   "quizMonthly" : JSON {month: "YYYY-MM", answered: N, correct: M}
//
// UX: correct answer gets a green flash + the explanation reveals; wrong
// answer turns the picked button red + reveals correct in green + explanation.
// "Next" button enables once the user has picked an answer.
(function setupQuiz() {
  const openBtn = document.getElementById('quiz-btn');
  const modal = document.getElementById('quiz-modal');
  if (!openBtn || !modal || !Array.isArray(QUIZ_POOL) || QUIZ_POOL.length === 0) return;
  const closeBtn = document.getElementById('quiz-modal-close');
  const catPill = document.getElementById('quiz-cat-pill');
  const qEl = document.getElementById('quiz-question');
  const optsEl = document.getElementById('quiz-options');
  const revealEl = document.getElementById('quiz-reveal');
  const verdictEl = document.getElementById('quiz-reveal-verdict');
  const explainEl = document.getElementById('quiz-reveal-text');
  const scoreEl = document.getElementById('quiz-score-num');
  const nextBtn = document.getElementById('quiz-next');

  const SEEN_KEY = 'quizSeen';
  const MONTHLY_KEY = 'quizMonthly';

  // ---- State engine (load / save / pick / record) -----------------------
  function nowMonth() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  }
  function loadState() {
    let seen = [], monthly = {month: nowMonth(), answered: 0, correct: 0};
    try {
      const s = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]');
      if (Array.isArray(s)) seen = s.filter(x => typeof x === 'number');
      const m = JSON.parse(localStorage.getItem(MONTHLY_KEY) || 'null');
      if (m && typeof m === 'object' && typeof m.month === 'string') {
        monthly = {month: m.month, answered: m.answered|0, correct: m.correct|0};
      }
    } catch (e) { /* private mode or corrupted entries */ }
    // Monthly auto-reset on calendar month flip
    if (monthly.month !== nowMonth()) {
      monthly = {month: nowMonth(), answered: 0, correct: 0};
    }
    return {seen, monthly};
  }
  function saveState(state) {
    try {
      localStorage.setItem(SEEN_KEY, JSON.stringify(state.seen));
      localStorage.setItem(MONTHLY_KEY, JSON.stringify(state.monthly));
    } catch (e) { /* private mode -- silently degrade */ }
  }
  let lastShownId = null;
  function pickNext(state) {
    // Recycle seen-set when 90%+ has been seen so the experience never dead-ends.
    if (state.seen.length >= Math.floor(QUIZ_POOL.length * 0.9)) {
      state.seen = [];
    }
    const unseen = QUIZ_POOL.filter(q => !state.seen.includes(q.id));
    let pool = unseen.length > 0 ? unseen : QUIZ_POOL;
    // Never show the same question twice in a row (across opens + Next clicks).
    // `seen` only grows on ANSWER, so without this an open-without-answering
    // re-rolls the same pool and can repeat the question you just saw.
    if (pool.length > 1 && lastShownId !== null) {
      const filtered = pool.filter(q => q.id !== lastShownId);
      if (filtered.length > 0) pool = filtered;
    }
    const q = pool[Math.floor(Math.random() * pool.length)];
    lastShownId = q.id;
    return q;
  }

  let state = loadState();
  let currentQ = null;
  let answered = false;

  // ---- Rendering --------------------------------------------------------
  function updateScore(animate) {
    scoreEl.textContent = state.monthly.correct + '/' + state.monthly.answered;
    if (animate) {
      scoreEl.classList.remove('pop');
      // Force reflow so the keyframe restarts. Tiny perf cost; fires once per answer.
      void scoreEl.offsetWidth;
      scoreEl.classList.add('pop');
    }
  }
  function renderQuestion(q) {
    currentQ = q;
    answered = false;
    catPill.textContent = q.category;
    qEl.textContent = q.question;
    revealEl.hidden = true;
    nextBtn.disabled = true;
    optsEl.innerHTML = '';
    q.options.forEach((opt, idx) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'quiz-option';
      b.textContent = opt;
      b.setAttribute('role', 'radio');
      b.addEventListener('click', () => handleAnswer(idx));
      optsEl.appendChild(b);
    });
  }
  function handleAnswer(picked) {
    if (answered) return;
    answered = true;
    const correct = currentQ.correct;
    const isCorrect = picked === correct;
    // Disable all + mark states
    Array.from(optsEl.children).forEach((btn, idx) => {
      btn.disabled = true;
      if (idx === correct) {
        btn.classList.add('correct');
      } else if (idx === picked) {
        btn.classList.add('incorrect');
      } else {
        btn.classList.add('dimmed');
      }
    });
    // Reveal explanation
    verdictEl.textContent = isCorrect ? 'Correct' : 'Not quite';
    verdictEl.className = 'quiz-reveal-verdict ' + (isCorrect ? 'pos' : 'neg');
    explainEl.textContent = currentQ.explanation;
    revealEl.hidden = false;
    nextBtn.disabled = false;
    // Persist state
    if (!state.seen.includes(currentQ.id)) state.seen.push(currentQ.id);
    state.monthly.answered++;
    if (isCorrect) state.monthly.correct++;
    saveState(state);
    updateScore(isCorrect);
  }
  function openQuiz() {
    state = loadState();   // re-read in case another tab updated
    updateScore(false);
    renderQuestion(pickNext(state));
    modal.removeAttribute('hidden');
    document.body.classList.add('modal-open');
  }
  function closeQuiz() {
    modal.setAttribute('hidden', '');
    document.body.classList.remove('modal-open');
  }

  // ---- Wiring ----------------------------------------------------------
  openBtn.addEventListener('click', openQuiz);
  closeBtn.addEventListener('click', closeQuiz);
  nextBtn.addEventListener('click', () => renderQuestion(pickNext(state)));
  // Backdrop click closes (the modal's .modal pseudo-element acts as backdrop)
  modal.addEventListener('click', (e) => { if (e.target === modal) closeQuiz(); });
  // ESC closes (only when the quiz modal is the topmost modal)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.hasAttribute('hidden')) closeQuiz();
  });
})();

// ---- Customizable module layout ---------------------------------------
// Sections below the hero are wrapped as draggable/hideable "modules".
// Order + hidden state persist in localStorage so each visitor's layout
// survives rebuilds (build.py only ships the default order). A new section
// the author adds later slots into its default position automatically, so
// people who cloned + customized never silently lose newly-shipped sections.
(function setupLastLook() {
  const KEY = 'stocks-dashboard-lastlook', DKEY = KEY + '-dismissed';
  const el = document.getElementById('last-look');
  if (!el || typeof LAST_LOOK === 'undefined' || !LAST_LOOK) return;
  let prev = null;
  try { prev = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) {}
  const store = () => { try { localStorage.setItem(KEY, JSON.stringify(LAST_LOOK)); } catch (e) {} };
  if (!prev) { store(); return; }                       // first visit
  if (prev.build_id === LAST_LOOK.build_id) return;       // already saw this build
  let dismissed = null;
  try { dismissed = localStorage.getItem(DKEY); } catch (e) {}
  if (dismissed === String(LAST_LOOK.build_id)) { store(); return; }
  const items = [];
  const dB = LAST_LOOK.basket_return - (prev.basket_return || 0);
  if (Math.abs(dB) >= 0.1) items.push(`basket <b>${dB >= 0 ? '+' : ''}${dB.toFixed(1)}pp</b>`);
  const prevIdeas = prev.idea_tickers || [];
  const newIdeas = (LAST_LOOK.idea_tickers || []).filter(t => !prevIdeas.includes(t));
  if (newIdeas.length) items.push(`<b>${newIdeas.length}</b> new idea${newIdeas.length > 1 ? 's' : ''}: ${newIdeas.join(', ')}`);
  const prevValue = prev.value_tickers || [];
  const newValue = (LAST_LOOK.value_tickers || []).filter(t => !prevValue.includes(t));
  if (newValue.length) items.push(`<b>${newValue.length}</b> new value pick${newValue.length > 1 ? 's' : ''}: ${newValue.join(', ')}`);
  const pm = [], pp = LAST_LOOK.predictions || {}, ppp = prev.predictions || {};
  for (const k in pp) { if (k in ppp) { const d = pp[k] - ppp[k]; if (Math.abs(d) >= 3) pm.push(`${k} ${d >= 0 ? '+' : ''}${d.toFixed(0)}pp`); } }
  if (pm.length) items.push(pm.join(' &middot; '));
  store();
  if (!items.length) return;
  el.innerHTML = '<span class="ll-tag">Since your last visit</span>'
    + '<span class="ll-body">' + items.join(' &middot; ') + '</span>'
    + '<button class="ll-x" aria-label="Dismiss">&times;</button>';
  el.hidden = false;
  el.querySelector('.ll-x').addEventListener('click', () => {
    el.hidden = true;
    try { localStorage.setItem(DKEY, String(LAST_LOOK.build_id)); } catch (e) {}
  });
})();

(function setupLayout() {
  const KEY = 'stocks-dashboard-layout-v1';
  const stack = document.getElementById('module-stack');
  if (!stack) return;
  const editBtn = document.getElementById('edit-layout-btn');
  const resetBtn = document.getElementById('reset-layout-btn');
  const defaultOrder = (stack.dataset.defaultOrder || '').split(',').filter(Boolean);

  const mods = () => Array.from(stack.querySelectorAll(':scope > .module'));
  const modById = (id) => stack.querySelector(':scope > .module[data-module="' + id + '"]');

  // Modules that should pair side-by-side when they're adjacent and both
  // visible. Re-derived from the live DOM after every layout change so the
  // pairing is a pure function of the current order + hidden state — no
  // separate persistence needed.
  const PAIR_MEMBERS = ['outlook', 'news'];
  function applyPairing() {
    stack.querySelectorAll('.module-paired').forEach(el => el.classList.remove('module-paired'));
    const a = modById(PAIR_MEMBERS[0]);
    const b = modById(PAIR_MEMBERS[1]);
    if (!a || !b) return;
    if (a.dataset.hidden === 'true' || b.dataset.hidden === 'true') return;
    const children = Array.from(stack.children);
    const aIdx = children.indexOf(a);
    const bIdx = children.indexOf(b);
    if (aIdx < 0 || bIdx < 0) return;
    if (Math.abs(aIdx - bIdx) === 1) {
      a.classList.add('module-paired');
      b.classList.add('module-paired');
    }
  }

  function load() {
    try {
      const s = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (!s || !Array.isArray(s.order)) return null;
      return { order: s.order, hidden: Array.isArray(s.hidden) ? s.hidden : [] };
    } catch (e) { return null; }
  }
  function save() {
    const order = mods().map(el => el.dataset.module);
    const hidden = mods().filter(el => el.dataset.hidden === 'true').map(el => el.dataset.module);
    try { localStorage.setItem(KEY, JSON.stringify({ order, hidden })); } catch (e) {}
  }

  function applyOrder(savedOrder) {
    const present = new Set(mods().map(el => el.dataset.module));
    const result = savedOrder.filter(id => present.has(id));
    const placed = new Set(result);
    // Slot any module missing from the saved order (newly shipped) into the
    // position it occupies in the default order.
    defaultOrder.forEach(id => {
      if (!present.has(id) || placed.has(id)) return;
      const di = defaultOrder.indexOf(id);
      let after = null;
      for (let i = di - 1; i >= 0; i--) {
        if (placed.has(defaultOrder[i])) { after = defaultOrder[i]; break; }
      }
      if (after === null) result.unshift(id);
      else result.splice(result.indexOf(after) + 1, 0, id);
      placed.add(id);
    });
    result.forEach(id => { const el = modById(id); if (el) stack.appendChild(el); });
  }
  function applyHidden(hiddenArr) {
    const h = new Set(hiddenArr);
    mods().forEach(el => {
      const hide = h.has(el.dataset.module);
      el.dataset.hidden = hide ? 'true' : 'false';
      const cb = el.querySelector('.module-vis-cb');
      if (cb) cb.checked = !hide;
      const txt = el.querySelector('.module-vis-txt');
      if (txt) txt.textContent = hide ? 'Hidden' : 'Shown';
    });
  }

  const state = load();
  if (state) { applyOrder(state.order); applyHidden(state.hidden); }
  else { applyHidden([]); }
  applyPairing();

  let sortable = null;
  function enterEdit() {
    document.body.classList.add('edit-mode');
    // v2.1 icon-button fix: update the data-tooltip (which the CSS hover
    // tooltip reads) instead of textContent (which would wipe the SVG icon).
    if (editBtn) { editBtn.dataset.tooltip = 'Done editing'; editBtn.classList.add('active'); editBtn.setAttribute('aria-pressed', 'true'); }
    if (resetBtn) resetBtn.hidden = false;
    if (window.Sortable && !sortable) {
      sortable = window.Sortable.create(stack, {
        handle: '.module-grip', draggable: '.module', animation: 150,
        ghostClass: 'module-ghost', chosenClass: 'module-chosen',
        onEnd: () => { save(); applyPairing(); },
      });
    }
  }
  function exitEdit() {
    document.body.classList.remove('edit-mode');
    if (editBtn) { editBtn.dataset.tooltip = 'Edit layout'; editBtn.classList.remove('active'); editBtn.setAttribute('aria-pressed', 'false'); }
    if (resetBtn) resetBtn.hidden = true;
    if (sortable) { sortable.destroy(); sortable = null; }
  }
  if (editBtn) editBtn.addEventListener('click', () => {
    if (document.body.classList.contains('edit-mode')) exitEdit(); else enterEdit();
  });

  // One-time discovery hint: pulse the edit button + show a tooltip on the
  // first ever page load. Gated on a localStorage flag so returning visitors
  // never see it again. Auto-dismisses after 8s OR on any click anywhere.
  //
  // Tooltip position is computed from the button's actual viewport rect so
  // the arrow lines up regardless of topbar contents -- a prior version
  // hardcoded `right:24px` which (incorrectly) ended up pointing at the
  // palette buttons because the topbar is left-aligned within its container.
  (function maybeShowDiscoveryHint() {
    if (!editBtn) return;
    const DISCOVERED_KEY = 'edit-layout-discovered';
    try { if (localStorage.getItem(DISCOVERED_KEY)) return; }
    catch (e) { return; }  // privacy mode: skip rather than crash
    const tip = document.getElementById('edit-tooltip');
    editBtn.classList.add('pulse');
    if (tip) {
      // Anchor the tooltip's top-left ~8px below + aligned with the button's
      // left edge. The CSS arrow sits at left:18px, so it points up at the
      // button. Slight left-shift (10px) puts the arrow under the button's
      // center rather than its leftmost pixel for a softer visual anchor.
      const r = editBtn.getBoundingClientRect();
      tip.style.top  = (r.bottom + 8) + 'px';
      tip.style.left = Math.max(8, r.left - 10) + 'px';
      tip.style.right = 'auto';
      tip.hidden = false;
    }
    let dismissed = false;
    function dismiss() {
      if (dismissed) return;
      dismissed = true;
      editBtn.classList.remove('pulse');
      if (tip) tip.hidden = true;
      try { localStorage.setItem(DISCOVERED_KEY, '1'); } catch (e) {}
    }
    // Dismiss triggers: clicking anywhere, clicking the tooltip itself,
    // or 8s timeout (whichever comes first).
    document.addEventListener('click', dismiss, { once: true, capture: true });
    if (tip) tip.addEventListener('click', dismiss, { once: true });
    setTimeout(dismiss, 8000);
  })();

  stack.addEventListener('change', (e) => {
    const cb = e.target.closest && e.target.closest('.module-vis-cb');
    if (!cb) return;
    const mod = cb.closest('.module');
    if (!mod) return;
    const hide = !cb.checked;
    mod.dataset.hidden = hide ? 'true' : 'false';
    const txt = mod.querySelector('.module-vis-txt');
    if (txt) txt.textContent = hide ? 'Hidden' : 'Shown';
    save();
    applyPairing();
  });

  if (resetBtn) resetBtn.addEventListener('click', () => {
    try { localStorage.removeItem(KEY); } catch (e) {}
    defaultOrder.forEach(id => { const el = modById(id); if (el) stack.appendChild(el); });
    applyHidden([]);
    applyPairing();
  });
})();

// T8: Hero stats picker. Independent of setupLayout to keep responsibilities
// isolated (modules vs stats can be edited / reset / persisted independently).
// localStorage schema: {"selected": ["slug1", "slug2", ...]} -- an ordered
// array of currently-visible stat slugs. Anything not in `selected` is hidden
// outside edit-mode; in edit-mode all 10 stats are visible (greyed if hidden)
// so the user can toggle them on. Sortable drag in edit-mode reorders.
(function setupStats() {
  const KEY = 'stocks-dashboard-stats-v1';
  const grid = document.getElementById('stats-grid');
  if (!grid) return;
  const defaultSelected = (grid.dataset.statsDefault || '').split(',').filter(Boolean);
  const allSlugs = (grid.dataset.statsAll || '').split(',').filter(Boolean);

  const cards = () => Array.from(grid.querySelectorAll(':scope > .stat'));

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed && Array.isArray(parsed.selected)) {
        // Drop unknown slugs (forward-compat for future stats added/removed).
        return parsed.selected.filter(s => allSlugs.includes(s));
      }
    } catch (e) {}
    return null;
  }
  function save(selected) {
    try { localStorage.setItem(KEY, JSON.stringify({ selected: selected })); }
    catch (e) {}
  }
  function apply(selected) {
    const orderMap = {};
    selected.forEach((s, i) => { orderMap[s] = i; });
    cards().forEach(card => {
      const slug = card.dataset.stat;
      const isShown = selected.includes(slug);
      card.dataset.statHidden = isShown ? 'false' : 'true';
      // CSS `order` reorders without touching DOM, plays nice with Sortable.
      card.style.order = isShown ? String(orderMap[slug]) : '99';
      const cb = card.querySelector('.stat-vis-cb');
      if (cb) cb.checked = isShown;
      const txt = card.querySelector('.stat-vis-txt');
      if (txt) txt.textContent = isShown ? 'Shown' : 'Hidden';
    });
  }

  apply(load() || defaultSelected);

  grid.addEventListener('change', (e) => {
    const cb = e.target.closest && e.target.closest('.stat-vis-cb');
    if (!cb) return;
    const card = cb.closest('.stat');
    if (!card) return;
    const slug = card.dataset.stat;
    const cur = load() || defaultSelected.slice();
    const next = cb.checked
      ? (cur.includes(slug) ? cur : cur.concat([slug]))
      : cur.filter(s => s !== slug);
    apply(next);
    save(next);
  });

  // Drag-to-reorder available in edit-mode only. SortableJS reads visual
  // order via card position; we then derive `selected` from the new order.
  let sortable = null;
  function attachSortable() {
    if (!window.Sortable || sortable) return;
    sortable = window.Sortable.create(grid, {
      handle: '.stat-grip', draggable: '.stat', animation: 150,
      ghostClass: 'stat-ghost', chosenClass: 'stat-chosen',
      onEnd: () => {
        // After drag, DOM order reflects user intent. Re-derive selected list
        // from the new visual order, keeping only currently-shown cards.
        const order = cards()
          .filter(c => c.dataset.statHidden !== 'true')
          .map(c => c.dataset.stat);
        save(order);
        apply(order);
      },
    });
  }
  function detachSortable() {
    if (sortable) { sortable.destroy(); sortable = null; }
  }
  // Observe body class changes (edit-mode toggle) instead of hooking the
  // edit button click directly -- avoids ordering coupling with setupLayout.
  const onEditChange = () => {
    if (document.body.classList.contains('edit-mode')) attachSortable();
    else detachSortable();
  };
  new MutationObserver(onEditChange).observe(document.body,
    { attributes: true, attributeFilter: ['class'] });
  onEditChange();

  // Reset button also clears stats state (one button, both layers).
  const resetBtn = document.getElementById('reset-layout-btn');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      try { localStorage.removeItem(KEY); } catch (e) {}
      apply(defaultSelected);
    });
  }
})();

// T7 cosmetic: alpha sparkline hover. Shows date + value of the nearest
// point in the header pill; vertical crosshair + dot mark the position.
// Defensive about missing element so this is a no-op when the sparkline
// itself was suppressed (less than 5 weeks of data).
(function setupAlphaHover() {
  const wrap = document.getElementById('alpha-sparkline-wrap');
  const svg  = document.getElementById('alpha-sparkline-svg');
  const latestEl = document.getElementById('alpha-sparkline-latest');
  if (!wrap || !svg || !latestEl) return;
  const dates  = (svg.dataset.dates  || '').split(',').filter(Boolean);
  const values = (svg.dataset.values || '').split(',').map(parseFloat);
  if (!dates.length || dates.length !== values.length) return;
  const cross = svg.querySelector('.alpha-cross');
  const dot   = svg.querySelector('.alpha-dot');
  const vb = svg.viewBox.baseVal;   // {x, y, width, height}
  const padX = 0, padY = 4;
  const vmin = Math.min(...values), vmax = Math.max(...values);
  const vrange = Math.max(vmax - vmin, 1e-9);
  const n = values.length;
  // v2.7 #6: map x by DATE over the shared [domainStart,domainEnd] span (same as
  // the build side) so hover crosshairs land on the calendar-correct point.
  const domStart = Date.parse((svg.dataset.domainStart || '') + 'T00:00:00');
  const domEnd   = Date.parse((svg.dataset.domainEnd   || '') + 'T00:00:00');
  const useDomain = !Number.isNaN(domStart) && !Number.isNaN(domEnd) && domEnd > domStart;
  const domSpan = Math.max(domEnd - domStart, 1);
  const dateMs = dates.map(d => Date.parse(d + 'T00:00:00'));
  const xAt = (i) => {
    if (!useDomain) return padX + i / Math.max(n - 1, 1) * (vb.width - 2 * padX);
    let f = (dateMs[i] - domStart) / domSpan; f = f < 0 ? 0 : (f > 1 ? 1 : f);
    return padX + f * (vb.width - 2 * padX);
  };
  const yAt = (v) => padY + (vmax - v) / vrange * (vb.height - 2 * padY);
  const defaultText = latestEl.dataset.defaultText;
  const defaultCls = latestEl.classList.contains('neg') ? 'neg' : 'pos';

  function relMonth(dateStr) {
    // Compact date for the head pill, e.g. "12 Mar 25".
    const d = new Date(dateStr + 'T00:00:00');
    if (isNaN(d.getTime())) return dateStr;
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${d.getDate()} ${months[d.getMonth()]} ${String(d.getFullYear()).slice(-2)}`;
  }

  svg.addEventListener('mousemove', (e) => {
    const rect = svg.getBoundingClientRect();
    const vbX = (e.clientX - rect.left) / rect.width * vb.width;
    // Nearest index in the dataset
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < n; i++) {
      const d = Math.abs(xAt(i) - vbX);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    const v = values[best];
    cross.setAttribute('x1', xAt(best));
    cross.setAttribute('x2', xAt(best));
    cross.setAttribute('opacity', '0.6');
    dot.setAttribute('cx', xAt(best));
    dot.setAttribute('cy', yAt(v));
    dot.setAttribute('opacity', '1');
    latestEl.textContent = `${relMonth(dates[best])} · ${v >= 0 ? '+' : ''}${v.toFixed(1)} pp`;
    latestEl.classList.remove('pos', 'neg');
    latestEl.classList.add(v >= 0 ? 'pos' : 'neg');
  });

  svg.addEventListener('mouseleave', () => {
    cross.setAttribute('opacity', '0');
    dot.setAttribute('opacity', '0');
    latestEl.textContent = defaultText;
    latestEl.classList.remove('pos', 'neg');
    latestEl.classList.add(defaultCls);
  });
})();

// v1.9 #1: Drawdown sparkline hover. Mirrors setupAlphaHover() but drawdown
// values are bounded [min_dd, 0] (never above peak), so the head pill renders
// a plain "-X.X%" with no sign-flip styling -- always var(--down).
(function setupDrawdownHover() {
  const wrap = document.getElementById('dd-sparkline-wrap');
  const svg  = document.getElementById('dd-sparkline-svg');
  const latestEl = document.getElementById('dd-sparkline-latest');
  if (!wrap || !svg || !latestEl) return;
  const dates  = (svg.dataset.dates  || '').split(',').filter(Boolean);
  const values = (svg.dataset.values || '').split(',').map(parseFloat);
  if (!dates.length || dates.length !== values.length) return;
  const cross = svg.querySelector('.dd-cross');
  const dot   = svg.querySelector('.dd-dot');
  const vb = svg.viewBox.baseVal;
  const padX = 0, padY = 3;
  // Match the build-side _dx / _dy mapping (DH-2*padY active height, value
  // floor = min(values + [0])). Important: values are <= 0, so y=padY is the
  // 0% top of the chart and y=DH-padY is the worst drawdown.
  const ddMin = Math.min(...values, 0);
  const ddRange = Math.max(Math.abs(ddMin), 1e-9);
  const n = values.length;
  // v2.7 #6: date-domain x mapping (mirror of the alpha sparkline + build side).
  const domStart = Date.parse((svg.dataset.domainStart || '') + 'T00:00:00');
  const domEnd   = Date.parse((svg.dataset.domainEnd   || '') + 'T00:00:00');
  const useDomain = !Number.isNaN(domStart) && !Number.isNaN(domEnd) && domEnd > domStart;
  const domSpan = Math.max(domEnd - domStart, 1);
  const dateMs = dates.map(d => Date.parse(d + 'T00:00:00'));
  const xAt = (i) => {
    if (!useDomain) return padX + i / Math.max(n - 1, 1) * (vb.width - 2 * padX);
    let f = (dateMs[i] - domStart) / domSpan; f = f < 0 ? 0 : (f > 1 ? 1 : f);
    return padX + f * (vb.width - 2 * padX);
  };
  const yAt = (v) => padY + (-v) / ddRange * (vb.height - 2 * padY);
  const defaultText = latestEl.dataset.defaultText;

  function relMonth(dateStr) {
    const d = new Date(dateStr + 'T00:00:00');
    if (isNaN(d.getTime())) return dateStr;
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${d.getDate()} ${months[d.getMonth()]} ${String(d.getFullYear()).slice(-2)}`;
  }

  svg.addEventListener('mousemove', (e) => {
    const rect = svg.getBoundingClientRect();
    const vbX = (e.clientX - rect.left) / rect.width * vb.width;
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < n; i++) {
      const d = Math.abs(xAt(i) - vbX);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    const v = values[best];
    cross.setAttribute('x1', xAt(best));
    cross.setAttribute('x2', xAt(best));
    cross.setAttribute('opacity', '0.6');
    dot.setAttribute('cx', xAt(best));
    dot.setAttribute('cy', yAt(v));
    dot.setAttribute('opacity', '1');
    latestEl.textContent = `${relMonth(dates[best])} · ${v.toFixed(1)}%`;
  });

  svg.addEventListener('mouseleave', () => {
    cross.setAttribute('opacity', '0');
    dot.setAttribute('opacity', '0');
    latestEl.textContent = defaultText;
  });
})();

// ---- Live news refresh via Cloudflare Worker --------------------------
// The static news box is rendered server-side at build time as a fallback.
// If NEWS_WORKER_URL is set, fetch fresh items on page load and swap them in.
// On any failure (Worker down, network blip, CORS issue), the static fallback
// stays untouched — silent graceful degradation.
// (NEWS_WORKER_URL is declared in the v3.5 generated-const prelude above.)

async function refreshNewsFromWorker() {
  if (!NEWS_WORKER_URL) return;
  try {
    const resp = await fetch(NEWS_WORKER_URL, {cache: 'no-cache'});
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data || !Array.isArray(data.items) || data.items.length === 0) return;
    renderLiveNews(data.items, data.fetched_at);
  } catch (e) { /* keep fallback */ }
}

function renderLiveNews(items, fetchedAt) {
  const list = document.querySelector('.news-list');
  if (!list) return;
  list.innerHTML = items.map(it => {
    const when = relativeNewsTime(new Date(it.published));
    return `<a class="news-row" data-source="${escapeNewsHtml(it.source)}" href="${safeUrl(it.link)}" target="_blank" rel="noopener noreferrer">`
      + `<div class="news-title">${escapeNewsHtml(it.title)}</div>`
      + `<div class="news-meta"><span class="news-src">${escapeNewsHtml(it.source)}</span>`
      + `<span class="news-dot">·</span><span class="news-when">${escapeNewsHtml(when)}</span></div>`
      + `</a>`;
  }).join('');
  // Rebuild source chips from the live data — sources can change as feeds
  // are tuned on the Worker side, so don't trust the static fallback's list.
  const sources = [...new Set(items.map(it => it.source))];
  rebuildNewsChips(sources);
  const stale = document.querySelector('.news-stale');
  if (stale && fetchedAt) {
    const t = new Date(fetchedAt);
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const pad = n => String(n).padStart(2,'0');
    stale.textContent = `live · ${pad(t.getUTCDate())} ${months[t.getUTCMonth()]} ${pad(t.getUTCHours())}:${pad(t.getUTCMinutes())} UTC`;
    stale.classList.add('news-live');
  }
}

function rebuildNewsChips(sources) {
  const chipBar = document.querySelector('.news-chips');
  if (!chipBar) return;
  const saved = (() => { try { return localStorage.getItem('stocks-dashboard-news-source'); } catch (e) { return null; } })();
  const current = saved || '*';
  const html = ['<button class="news-chip" data-src="*">All</button>']
    .concat(sources.map(s => `<button class="news-chip" data-src="${escapeNewsHtml(s)}">${escapeNewsHtml(s)}</button>`))
    .join('');
  chipBar.innerHTML = html;
  applyNewsFilter(current);
  chipBar.querySelectorAll('.news-chip').forEach(btn => {
    btn.addEventListener('click', () => applyNewsFilter(btn.dataset.src));
  });
}

function applyNewsFilter(src) {
  const chipBar = document.querySelector('.news-chips');
  if (!chipBar) return;
  const rows = [...document.querySelectorAll('.news-row')];
  // Resilience: a saved/selected source that matches NO current rows (stale
  // localStorage, or the live worker's feed set changed) would hide every row
  // and leave the panel empty. Fall back to "All" so news never renders blank.
  if (src !== '*' && !rows.some(r => r.dataset.source === src)) src = '*';
  chipBar.querySelectorAll('.news-chip').forEach(b => {
    b.classList.toggle('active', b.dataset.src === src);
  });
  rows.forEach(row => {
    const match = src === '*' || row.dataset.source === src;
    if (match) row.removeAttribute('hidden'); else row.setAttribute('hidden', '');
  });
  try { localStorage.setItem('stocks-dashboard-news-source', src); } catch (e) { /* ignore */ }
}

// Wire the chip cluster on the static fallback so it works before the Worker
// fetch (or if the Worker is unreachable). renderLiveNews() rebuilds it later.
document.querySelectorAll('.news-chips .news-chip').forEach(btn => {
  btn.addEventListener('click', () => applyNewsFilter(btn.dataset.src));
});
// Apply any saved filter to the static content on initial paint.
(function applySavedNewsFilter() {
  let saved = null;
  try { saved = localStorage.getItem('stocks-dashboard-news-source'); } catch (e) {}
  if (saved && saved !== '*') applyNewsFilter(saved);
})();

function relativeNewsTime(date) {
  const secs = Math.floor((Date.now() - date.getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  const days = Math.floor(secs / 86400);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString('en-GB', {day: '2-digit', month: 'short'});
}

function escapeNewsHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

// Only allow http(s) hrefs from untrusted feed/worker links — a javascript:/data:
// URL has no <>&" to entity-escape, so escaping alone leaves it clickable.
function safeUrl(u) {
  const s = String(u == null ? '' : u).trim();
  return /^https?:\/\//i.test(s) ? escapeNewsHtml(s) : '#';
}

refreshNewsFromWorker();
