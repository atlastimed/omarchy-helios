import QtQuick

// Sleep stages across the night, one lane per stage (awake on top, deep at the
// bottom), joined by thin risers where the stage changes. Lane position, not
// only colour, tells the stages apart. A span the strap left unstaged (it
// keeps 50 stages a night and folds the rest together) is a faint band across
// every lane instead of a claim about any one.
Canvas {
  id: root

  property var stages: []          // [start, end, "awake" | "rem" | "light" | "deep" | "unstaged"]
  property color unstagedColor: Qt.rgba(1, 1, 1, 0.1)
  property color markerColor: "white"
  property real start: 0
  property real end: 1
  property var colors: ({ awake: "red", rem: "violet", light: "gray", deep: "blue" })
  property color riserColor: Qt.rgba(1, 1, 1, 0.18)
  property real radius: 2

  readonly property var lanes: ["awake", "rem", "light", "deep"]

  // The stage under the pointer: its index in `stages`, or -1.
  property int hovered: -1
  readonly property var hoveredStage: hovered >= 0 && hovered < stages.length ? stages[hovered] : null
  property real pointerX: 0
  property real pointerY: 0

  function indexAt(x) {
    if (!stages || width <= 0) return -1
    var t = start + x / width * (end - start)
    for (var i = 0; i < stages.length; i++) if (t >= stages[i][0] && t < stages[i][1]) return i
    return -1
  }
  readonly property real laneHeight: height / lanes.length

  function xFor(t) { return (t - start) / Math.max(1, end - start) * width }
  function laneTop(stage) { return Math.max(0, lanes.indexOf(stage)) * laneHeight }

  onStagesChanged: {
    hovered = -1
    requestPaint()
  }
  onColorsChanged: requestPaint()
  onRiserColorChanged: requestPaint()
  onUnstagedColorChanged: requestPaint()
  onRadiusChanged: requestPaint()
  onStartChanged: requestPaint()
  onEndChanged: requestPaint()
  onWidthChanged: requestPaint()
  onHeightChanged: requestPaint()

  onPaint: {
    var ctx = getContext("2d")
    ctx.reset()
    if (!stages || stages.length === 0 || width <= 0) return
    var inset = Math.max(1, laneHeight * 0.14)

    ctx.strokeStyle = root.riserColor
    ctx.lineWidth = 1
    for (var i = 1; i < stages.length; i++) {
      if (lanes.indexOf(stages[i][2]) < 0 || lanes.indexOf(stages[i - 1][2]) < 0) continue
      var x = Math.round(xFor(stages[i][0])) + 0.5
      var a = laneTop(stages[i - 1][2]) + laneHeight / 2
      var b = laneTop(stages[i][2]) + laneHeight / 2
      ctx.beginPath(); ctx.moveTo(x, a); ctx.lineTo(x, b); ctx.stroke()
    }

    for (var s = 0; s < stages.length; s++) {
      var stage = stages[s]
      var x0 = xFor(stage[0]), x1 = Math.max(x0 + 1.5, xFor(stage[1]))
      if (lanes.indexOf(stage[2]) < 0) {
        ctx.fillStyle = root.unstagedColor
        ctx.fillRect(x0, inset, x1 - x0, height - inset * 2)
        continue
      }
      var y0 = laneTop(stage[2]) + inset, h = laneHeight - inset * 2
      var r = Math.min(root.radius, (x1 - x0) / 2, h / 2)
      ctx.fillStyle = root.colors[stage[2]] || root.colors.light
      ctx.beginPath()
      ctx.moveTo(x0 + r, y0)
      ctx.arcTo(x1, y0, x1, y0 + h, r)
      ctx.arcTo(x1, y0 + h, x0, y0 + h, r)
      ctx.arcTo(x0, y0 + h, x0, y0, r)
      ctx.arcTo(x0, y0, x1, y0, r)
      ctx.closePath()
      ctx.fill()
    }
  }

  // The moment being pointed at: the tip names the stage at that time,
  // whichever lane the pointer happens to be in.
  Rectangle {
    visible: root.hoveredStage !== null
    x: Math.round(root.pointerX)
    width: 1
    height: parent.height
    color: root.markerColor
    opacity: 0.45
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
