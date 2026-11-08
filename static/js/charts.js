// The dashboard's charts (M8b.5, ADR-0027). CSP-safe like app.js: no inline code, no eval, no
// style element. Each canvas[data-chart] holds its data as JSON in that attribute (htmx removes
// every <script> from the fragments it swaps in, so a json_script block would not arrive); the
// colours are the CSS tokens of the current theme, read with getComputedStyle; numbers and dates
// are formatted with Intl for <html lang>. The data table under each chart stays open until
// the chart is drawn, so a browser that runs none of this still shows the numbers.
(function () {
  "use strict";

  if (!window.Chart) {
    return;
  }
  var Chart = window.Chart;
  var root = document.documentElement;
  var lang = root.lang || "en";
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  var dark = window.matchMedia("(prefers-color-scheme: dark)");
  var drawn = new Map(); // canvas -> Chart

  // A token's colour as rgb()/rgba(): the browser resolves oklch() on a 1x1 canvas, so the
  // chart library never has to parse a colour syntax it may not know.
  var probe = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  probe.canvas.width = 1;
  probe.canvas.height = 1;
  function token(name) {
    var value = getComputedStyle(root).getPropertyValue(name).trim();
    probe.clearRect(0, 0, 1, 1);
    probe.fillStyle = "#000";
    probe.fillStyle = value || "#000";
    probe.fillRect(0, 0, 1, 1);
    var p = probe.getImageData(0, 0, 1, 1).data;
    return "rgba(" + p[0] + "," + p[1] + "," + p[2] + "," + (p[3] / 255).toFixed(3) + ")";
  }

  function palette() {
    var series = [];
    for (var i = 1; i <= 6; i += 1) {
      series.push(token("--chart-" + i));
    }
    return {
      series: series,
      ink: token("--ink"),
      soft: token("--ink-soft"),
      line: token("--line"),
      raised: token("--surface-raised"),
      font: getComputedStyle(root).getPropertyValue("--font-sans").trim() || "sans-serif"
    };
  }

  // Series after the first carry a pattern as well as a colour (stripes, dots, cross-hatch, …),
  // so two series never differ by colour alone; the legend shows the same pattern.
  var MARKS = ["solid", "stripes", "dots", "cross", "vertical", "horizontal"];
  function pattern(color, mark, ground) {
    if (mark === "solid") {
      return color;
    }
    var tile = document.createElement("canvas");
    tile.width = 8;
    tile.height = 8;
    var c = tile.getContext("2d");
    c.fillStyle = color;
    c.fillRect(0, 0, 8, 8);
    c.strokeStyle = ground;
    c.fillStyle = ground;
    c.lineWidth = 1.5;
    c.beginPath();
    if (mark === "stripes") {
      c.moveTo(0, 8);
      c.lineTo(8, 0);
      c.moveTo(-2, 2);
      c.lineTo(2, -2);
      c.moveTo(6, 10);
      c.lineTo(10, 6);
    } else if (mark === "dots") {
      c.arc(4, 4, 1.4, 0, Math.PI * 2);
      c.fill();
    } else if (mark === "cross") {
      c.moveTo(0, 0);
      c.lineTo(8, 8);
      c.moveTo(8, 0);
      c.lineTo(0, 8);
    } else if (mark === "vertical") {
      c.moveTo(4, 0);
      c.lineTo(4, 8);
    } else {
      c.moveTo(0, 4);
      c.lineTo(8, 4);
    }
    c.stroke();
    return c.createPattern(tile, "repeat");
  }

  var POINTS = ["circle", "rectRot", "triangle", "rect", "star", "crossRot"];
  var DASHES = [[], [6, 4], [2, 3], [10, 3, 2, 3], [4, 4], [1, 2]];

  function dayLabel(iso) {
    var parts = iso.split("-");
    var day = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2]));
    return new Intl.DateTimeFormat(lang, { day: "numeric", month: "short", timeZone: "UTC" }).format(day);
  }

  function datasets(data, colors) {
    return data.series.map(function (series, index) {
      var color = colors.series[index % colors.series.length];
      var set = { label: series.label, data: series.data };
      if (data.type === "line") {
        set.borderColor = color;
        set.backgroundColor = color;
        set.borderWidth = 2;
        set.borderDash = DASHES[index % DASHES.length];
        set.tension = 0.3;
        set.pointStyle = POINTS[index % POINTS.length];
        set.pointRadius = 3;
        set.pointHoverRadius = 6;
        set.pointBorderColor = colors.raised;
        set.pointBorderWidth = 1;
      } else {
        var fill = pattern(color, MARKS[index % MARKS.length], colors.raised);
        set.backgroundColor = fill;
        set.hoverBackgroundColor = fill;
        set.borderColor = color;
        set.borderWidth = index ? 1 : 0;
        set.maxBarThickness = data.type === "hbar" ? 22 : 28;
        set.borderRadius = data.stacked ? 0 : 3;
        if (data.stacked) {
          set.stack = "all";
        }
      }
      return set;
    });
  }

  function axes(data, colors) {
    var value = {
      beginAtZero: true,
      stacked: data.stacked,
      border: { display: false },
      grid: { color: colors.line },
      ticks: { precision: 0, color: colors.soft }
    };
    var category = {
      stacked: data.stacked,
      grid: { display: false },
      border: { color: colors.line },
      ticks: { color: colors.soft, maxRotation: 0, autoSkipPadding: 12 }
    };
    if (data.type === "hbar") {
      category.ticks.autoSkip = false; // a ranking names every bar
      // A long name is shortened on the axis (the tooltip and the table keep it whole), so a
      // narrow chart never clips it.
      category.ticks.callback = function (index) {
        var label = String(this.getLabelForValue(index));
        var room = this.chart.width < 480 ? 16 : 30;
        return label.length > room ? label.slice(0, room - 1) + "\u2026" : label;
      };
      return { x: value, y: category };
    }
    return { x: category, y: value };
  }

  function config(data) {
    var colors = palette();
    Chart.defaults.font.family = colors.font;
    Chart.defaults.color = colors.soft;
    var labels = data.axis === "week" ? data.labels.map(dayLabel) : data.labels;
    var many = data.series.length > 1;
    return {
      type: data.type === "line" ? "line" : "bar",
      data: { labels: labels, datasets: datasets(data, colors) },
      options: {
        locale: lang,
        indexAxis: data.type === "hbar" ? "y" : "x",
        responsive: true,
        maintainAspectRatio: false,
        animation: reduced.matches ? false : { duration: 400 },
        interaction: { mode: "index", intersect: false },
        scales: axes(data, colors),
        plugins: {
          legend: {
            display: many,
            position: "top",
            align: "end",
            labels: { color: colors.soft, usePointStyle: data.type === "line", boxWidth: 14, boxHeight: 12 }
          },
          tooltip: {
            backgroundColor: colors.ink,
            titleColor: colors.raised,
            bodyColor: colors.raised,
            padding: 10,
            cornerRadius: 8,
            boxPadding: 4
          }
        }
      }
    };
  }

  // The keyboard reaches what hover shows: arrows move along the points, Home and End jump,
  // Escape (or leaving the canvas) hides the tooltip.
  function focusAt(chart, index) {
    var active = chart.data.datasets.map(function (_set, datasetIndex) {
      return { datasetIndex: datasetIndex, index: index };
    });
    chart.$focused = index;
    chart.setActiveElements(active);
    chart.tooltip.setActiveElements(active, { x: 0, y: 0 });
    chart.update();
  }
  function blur(chart) {
    chart.$focused = undefined;
    chart.setActiveElements([]);
    chart.tooltip.setActiveElements([], { x: 0, y: 0 });
    chart.update();
  }
  function onKey(event) {
    var chart = drawn.get(event.currentTarget);
    if (!chart) {
      return;
    }
    var last = chart.data.labels.length - 1;
    var at = chart.$focused === undefined ? -1 : chart.$focused;
    var keys = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: last };
    if (event.key === "Escape") {
      blur(chart);
      return;
    }
    if (!(event.key in keys) || last < 0) {
      return;
    }
    event.preventDefault();
    focusAt(chart, Math.max(0, Math.min(last, keys[event.key])));
  }
  function onBlur(event) {
    var chart = drawn.get(event.currentTarget);
    if (chart) {
      blur(chart);
    }
  }

  function draw(canvas) {
    if (drawn.has(canvas)) {
      return;
    }
    var data;
    try {
      data = JSON.parse(canvas.getAttribute("data-chart"));
    } catch (error) {
      return;
    }
    var box = canvas.closest("[data-chart-box]");
    if (box) {
      box.hidden = false;
    }
    drawn.set(canvas, new Chart(canvas, config(data)));
    canvas.addEventListener("keydown", onKey);
    canvas.addEventListener("blur", onBlur);
    var table = canvas.closest(".chart-card") && canvas.closest(".chart-card").querySelector("[data-chart-table]");
    // Folded when first drawn; a redraw (the theme changed) leaves it as the person set it.
    if (table && !table.hasAttribute("data-chart-seen")) {
      table.open = false;
      table.setAttribute("data-chart-seen", "");
    }
  }

  function drawIn(node) {
    if (!node || !node.querySelectorAll) {
      return;
    }
    node.querySelectorAll("canvas[data-chart]").forEach(draw);
  }

  function destroy(canvas) {
    var chart = drawn.get(canvas);
    if (chart) {
      chart.destroy();
      drawn.delete(canvas);
    }
  }

  // A theme change redraws every chart in the new colours (and patterns on the new ground).
  function redraw() {
    var canvases = Array.from(drawn.keys()); // a copy: draw() adds each one back
    canvases.forEach(function (canvas) {
      destroy(canvas);
      if (canvas.isConnected) {
        draw(canvas);
      }
    });
  }

  drawIn(document);
  // A panel swapped in by htmx: draw what it brought (a drawn canvas is skipped).
  document.addEventListener("htmx:afterSettle", function () {
    drawIn(document);
  });
  document.addEventListener("htmx:beforeCleanupElement", function (event) {
    var node = event.detail && event.detail.elt;
    if (node && node.matches && node.matches("canvas[data-chart]")) {
      destroy(node);
    } else if (node && node.querySelectorAll) {
      node.querySelectorAll("canvas[data-chart]").forEach(destroy);
    }
  });
  document.addEventListener("gmat:theme", function () {
    window.requestAnimationFrame(redraw);
  });
  dark.addEventListener("change", redraw);
  reduced.addEventListener("change", redraw);
})();
