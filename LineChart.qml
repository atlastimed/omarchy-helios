import QtQuick
import "Model.js" as Model

// A time series drawn as a mean line over a min–max band. Points are
// [time, mean, min, max] buckets; a gap of more than two buckets (the strap
// was off) breaks the line instead of bridging it. Under the pointer, the
// nearest bucket is `hovered`, marked with a rule and a dot.
Canvas {
  id: root

  property var points: []
  property real start: 0
  property real end: 1
  property real bucket: 600
  property real floor: NaN        // fixed lower bound, else fitted
  property real ceiling: NaN
  property color lineColor: "white"
  property color bandColor: Qt.rgba(1, 1, 1, 0.12)
  property color gridColor: Qt.rgba(1, 1, 1, 0.06)
  property real lineWidth: 1.5
  property var guides: []         // values to draw dashed rules at, behind the series
  property color markerColor: "white"

  // The bucket under the pointer: its index in `points`, or -1.
  property int hovered: -1
  readonly property var hoveredPoint: hovered >= 0 && hovered < points.length ? points[hovered] : null
  property real pointerX: 0
  property real pointerY: 0

  // The bucket drawn nearest a horizontal position, or -1 where the series has a gap.
  function indexAt(x) {
    if (width <= 0) return -1
    return Model.nearestPoint(points, start + x / width * (end - start), bucket)
  }

  readonly property var range: fitRange()

  function fitRange() {
    var lo = Infinity, hi = -Infinity
    for (var i = 0; i < points.length; i++) {
      lo = Math.min(lo, points[i][2])
      hi = Math.max(hi, points[i][3])
    }
    if (!isFinite(lo)) return [0, 1]
    var pad = Math.max(1, (hi - lo) * 0.12)
    return [isNaN(floor) ? lo - pad : floor, isNaN(ceiling) ? hi + pad : ceiling]
  }

  function xFor(t) { return (t - start) / Math.max(1, end - start) * width }
  function yFor(v) { return height - (v - range[0]) / Math.max(0.001, range[1] - range[0]) * height }

  onPointsChanged: {
    hovered = -1
    requestPaint()
  }
  onGuidesChanged: requestPaint()
  onLineColorChanged: requestPaint()
  onBandColorChanged: requestPaint()
  onGridColorChanged: requestPaint()
  onWidthChanged: requestPaint()
  onHeightChanged: requestPaint()

  onPaint: {
    var ctx = getContext("2d")
    ctx.reset()
    if (width <= 0 || height <= 0) return

    ctx.lineWidth = 1
    for (var g = 0; g < guides.length; g++) {
      var gy = Math.round(yFor(guides[g])) + 0.5
      // Dashed by hand: a short rule every few pixels.
      ctx.strokeStyle = root.gridColor
      ctx.beginPath()
      for (var dx = 0; dx < width; dx += 6) { ctx.moveTo(dx, gy); ctx.lineTo(Math.min(width, dx + 3), gy) }
      ctx.stroke()
    }
    if (!points || points.length === 0) return

    // Split into runs of contiguous buckets.
    var runs = [], run = []
    for (var i = 0; i < points.length; i++) {
      if (run.length && points[i][0] - run[run.length - 1][0] > bucket * 2.5) { runs.push(run); run = [] }
      run.push(points[i])
    }
    runs.push(run)

    for (var r = 0; r < runs.length; r++) {
      var pts = runs[r]
      var half = Math.max(1, xFor(start + bucket) - xFor(start)) / 2
      ctx.fillStyle = root.bandColor
      ctx.strokeStyle = root.lineColor
      ctx.lineWidth = root.lineWidth
      ctx.lineJoin = "round"
      ctx.lineCap = "round"
      if (pts.length === 1) {
        // One bucket on its own (worn briefly between gaps): a bucket-wide
        // box for its range and a dash for its mean.
        var x = xFor(pts[0][0])
        ctx.fillRect(x - half, yFor(pts[0][3]), half * 2, Math.max(1, yFor(pts[0][2]) - yFor(pts[0][3])))
        ctx.beginPath()
        ctx.moveTo(x - half, yFor(pts[0][1]))
        ctx.lineTo(x + half, yFor(pts[0][1]))
        ctx.stroke()
        continue
      }
      ctx.beginPath()
      ctx.moveTo(xFor(pts[0][0]), yFor(pts[0][3]))
      for (var a = 1; a < pts.length; a++) ctx.lineTo(xFor(pts[a][0]), yFor(pts[a][3]))
      for (var b = pts.length - 1; b >= 0; b--) ctx.lineTo(xFor(pts[b][0]), yFor(pts[b][2]))
      ctx.closePath()
      ctx.fill()

      ctx.beginPath()
      ctx.moveTo(xFor(pts[0][0]), yFor(pts[0][1]))
      for (var c = 1; c < pts.length; c++) ctx.lineTo(xFor(pts[c][0]), yFor(pts[c][1]))
      ctx.stroke()
    }

    var last = points[points.length - 1]
    ctx.fillStyle = root.lineColor
    ctx.beginPath()
    ctx.arc(xFor(last[0]), yFor(last[1]), root.lineWidth * 1.8, 0, Math.PI * 2)
    ctx.fill()
  }

  // The marker is two items over the canvas, so moving the pointer repaints
  // nothing.
  Rectangle {
    visible: root.hoveredPoint !== null
    x: root.hoveredPoint ? Math.round(root.xFor(root.hoveredPoint[0])) : 0
    width: 1
    height: parent.height
    color: root.markerColor
    opacity: 0.45
  }
  Rectangle {
    visible: root.hoveredPoint !== null
    width: Math.round(root.lineWidth * 4.5)
    height: width
    radius: width / 2
    x: root.hoveredPoint ? root.xFor(root.hoveredPoint[0]) - width / 2 : 0
    y: root.hoveredPoint ? root.yFor(root.hoveredPoint[1]) - height / 2 : 0
    color: root.lineColor
    border.width: 1
    border.color: root.markerColor
  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.NoButton
    onPositionChanged: function(mouse) {
      root.pointerX = mouse.x
      root.pointerY = mouse.y
      root.hovered = root.indexAt(mouse.x)
    }
    onExited: root.hovered = -1
  }
}
