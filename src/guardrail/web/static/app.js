// Two jobs. On an ordinary page: reload every minute, keeping the scroll
// position. On the asset page: draw the chart and refresh it in place, because
// reloading the page would throw away the zoom and the crosshair.
(function () {
  var strip = document.getElementById("live");
  if (strip) { startLive(strip); }

  var host = document.getElementById("chart");
  if (host) {
    loadLibrary(host);
    return;
  }

  var key = "gr-scroll:" + location.pathname + location.search;
  try {
    var saved = sessionStorage.getItem(key);
    if (saved) { window.scrollTo(0, Number(saved)); }
  } catch (e) { /* storage unavailable */ }
  setTimeout(function () {
    try { sessionStorage.setItem(key, String(window.scrollY)); } catch (e) { /* ignore */ }
    location.reload();
  }, 60000);

  // The price strip. Five seconds is the refresh the server caches for, so
  // polling faster would return the same numbers and cost nothing but noise.
  function startLive(el) {
    function paint() {
      fetch("/api/live", { headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) { throw new Error("HTTP " + r.status); }
          return r.json();
        })
        .then(function (d) {
          while (el.firstChild) { el.removeChild(el.firstChild); }
          d.quotes.forEach(function (q) {
            el.appendChild(part("sym", q.symbol.replace("USDT", "")));
            el.appendChild(part("px", format(q.last)));
            el.appendChild(part(
              "chg " + (q.change_pct >= 0 ? "up" : "down"),
              (q.change_pct >= 0 ? "+" : "") + q.change_pct.toFixed(2) + "% 24h"
            ));
            el.appendChild(part("range", "min " + format(q.low) + " · max " + format(q.high)));
          });
          var state = d.error
            ? "exchange fora do ar há " + Math.round(d.age_seconds) + "s: " + d.error
            : (d.stale ? "preço parado há " + Math.round(d.age_seconds) + "s" : "ao vivo");
          el.appendChild(part("live-state" + (d.error || d.stale ? " bad" : ""), state));
        })
        .catch(function (err) {
          while (el.firstChild) { el.removeChild(el.firstChild); }
          el.appendChild(part("live-state bad", "preço ao vivo indisponível: " + err.message));
        });
    }

    function part(cls, text) {
      var span = document.createElement("span");
      span.className = cls;
      span.textContent = text;
      return span;
    }

    function format(value) {
      if (value >= 1000) { return value.toFixed(0); }
      if (value >= 1) { return value.toFixed(2); }
      return value.toFixed(5);
    }

    paint();
    setInterval(paint, 5000);
  }

  // The library is only fetched on the page that needs it, and from this same
  // server: the content policy allows no other origin.
  function loadLibrary(el) {
    var tag = document.createElement("script");
    tag.src = "/static/lightweight-charts.js";
    tag.onload = function () { build(el); };
    tag.onerror = function () { say(el, "A biblioteca do gráfico não carregou."); };
    document.head.appendChild(tag);
  }

  function say(el, text) {
    var p = document.createElement("p");
    p.className = "asset-alert";
    p.textContent = text;
    el.parentNode.insertBefore(p, el);
  }

  function build(el) {
    var L = window.LightweightCharts;
    if (!L) { say(el, "A biblioteca do gráfico não carregou."); return; }

    // Colours come from the stylesheet, so the palette has one home. Writing
    // them again here would be a second source of truth that drifts.
    var sheet = getComputedStyle(document.documentElement);
    function token(name) { return sheet.getPropertyValue(name).trim(); }
    var ink = token("--ink"), soft = token("--ink-soft");
    var line = token("--line"), faint = token("--line-soft");
    var up = token("--up"), down = token("--down");
    var accent = token("--accent"), ochre = token("--warn-ink");
    var upSoft = token("--up-soft"), downSoft = token("--down-soft");
    var common = {
      layout: { background: { color: token("--card") }, textColor: ink, fontFamily: "inherit" },
      grid: { vertLines: { color: faint }, horzLines: { color: faint } },
      rightPriceScale: { borderColor: line },
      timeScale: { borderColor: line, timeVisible: true, secondsVisible: false },
      autoSize: true
    };

    var price = L.createChart(el, common);
    var rsiEl = document.getElementById("rsi");
    var rsiChart = rsiEl ? L.createChart(rsiEl, common) : null;

    var candles = price.addCandlestickSeries({
      upColor: up, downColor: down, borderUpColor: up, borderDownColor: down,
      wickUpColor: up, wickDownColor: down
    });
    var fast = price.addLineSeries({ color: accent, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    var slow = price.addLineSeries({ color: ochre, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    var volume = price.addHistogramSeries({
      priceScaleId: "volume", priceFormat: { type: "volume" },
      priceLineVisible: false, lastValueVisible: false
    });
    price.priceScale("volume").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    var rsi = null;
    if (rsiChart) {
      rsi = rsiChart.addLineSeries({ color: ink, lineWidth: 1, priceLineVisible: false });
      [30, 50, 70].forEach(function (level) {
        rsi.createPriceLine({
          price: level, color: level === 50 ? faint : line, lineWidth: 1,
          lineStyle: 2, axisLabelVisible: true, title: String(level)
        });
      });
      price.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
        if (range) { rsiChart.timeScale().setVisibleLogicalRange(range); }
      });
      rsiChart.timeScale().subscribeVisibleLogicalRangeChange(function (range) {
        if (range) { price.timeScale().setVisibleLogicalRange(range); }
      });
    }

    var drawn = [];
    function load(first) {
      var url = "/api/chart/" + encodeURIComponent(el.dataset.symbol) +
                "?faixa=" + encodeURIComponent(el.dataset.faixa);
      fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) { throw new Error("HTTP " + r.status); }
          return r.json();
        })
        .then(function (d) {
          candles.setData(d.candles);
          volume.setData(d.candles.map(function (c) {
            return { time: c.time, value: c.volume, color: c.close >= c.open ? upSoft : downSoft };
          }));
          fast.setData(d.ema_fast.points);
          slow.setData(d.ema_slow.points);
          if (rsi) { rsi.setData(d.rsi.points); }

          drawn.forEach(function (l) { candles.removePriceLine(l); });
          drawn = [];
          d.levels.forEach(function (lv) {
            drawn.push(candles.createPriceLine({
              price: lv.price, color: lv.kind === "resistance" ? down : up,
              lineWidth: 1, lineStyle: 2, axisLabelVisible: true,
              title: (lv.kind === "resistance" ? "R" : "S") + " " + lv.touches + "x"
            }));
          });
          if (d.profile) {
            drawn.push(candles.createPriceLine({
              price: d.profile.point_of_control, color: accent, lineWidth: 2,
              lineStyle: 0, axisLabelVisible: true, title: "POC"
            }));
          }
          if (d.reach.length) {
            var band = d.reach[d.reach.length - 1];
            [band.near_high, band.near_low].forEach(function (value) {
              drawn.push(candles.createPriceLine({
                price: value, color: soft, lineWidth: 1, lineStyle: 3,
                axisLabelVisible: false, title: "alcance " + band.horizon_days + "d"
              }));
            });
          }
          if (first) { price.timeScale().fitContent(); }
        })
        .catch(function (err) {
          say(el, "Não deu para carregar o gráfico: " + err.message);
        });
    }

    load(true);
    setInterval(function () { load(false); }, 60000);
  }
})();
