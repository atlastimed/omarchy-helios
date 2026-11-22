import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The reading under the pointer, as a small label beside it: a value in bold
// and a dimmer line saying when. Declared inside the chart it describes and
// positioned in that chart's coordinates.
//
// It is an item rather than a ToolTip popup so that it appears at once,
// follows the pointer, and is drawn with the chart; it wears the shell's
// tooltip colours and border.
BorderSurface {
  id: root

  property bool shown: false
  property string title: ""
  property string detail: ""
  // The pointer, in the parent's coordinates.
  property real pointerX: 0
  property real pointerY: 0
  property string fontFamily: Style.font.family

  readonly property real gap: Style.space(10)
  readonly property var spec: Border.localOrSurfaceSpec("tooltip", "border", Commons.Color.tooltip.border,
    Commons.Color.tooltip.border, Style.normalBorderWidth)
  readonly property real limit: parent ? parent.width : 0

  visible: shown && title !== ""
  z: 10
  color: Commons.Color.tooltip.background
  borderSpec: spec
  radius: Style.cornerRadius
  width: Math.ceil(labels.implicitWidth + Border.left(spec) + Border.right(spec) + Style.spacing.controlPaddingX * 2)
  height: Math.ceil(labels.implicitHeight + Border.top(spec) + Border.bottom(spec) + Style.spacing.controlPaddingY * 2)
  // To the right of the pointer while there is room, otherwise to its left;
  // above it, so the pointer never covers what it is reading.
  x: Math.round(pointerX + gap + width <= limit ? pointerX + gap : Math.max(0, pointerX - gap - width))
  y: Math.round(pointerY - height - gap)

  Column {
    id: labels
    x: Border.left(root.spec) + Style.spacing.controlPaddingX
    y: Border.top(root.spec) + Style.spacing.controlPaddingY
    spacing: Style.space(1)

    Text {
      textFormat: Text.PlainText
      text: root.title
      color: Commons.Color.tooltip.text
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }
    Text {
      visible: text !== ""
      textFormat: Text.PlainText
      text: root.detail
      color: Qt.darker(Commons.Color.tooltip.text, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
