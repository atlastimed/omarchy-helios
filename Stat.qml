import QtQuick
import qs.Commons

// A labelled reading: small-caps caption over a value with an optional unit,
// the composition the weather panel uses for FEELS / WIND / HUMID.
Column {
  id: root

  property string label: ""
  property string value: "—"
  property string unit: ""
  property string note: ""
  property color foreground: "white"
  property string fontFamily: Style.font.family
  property real valueSize: Style.font.title
  // Elide the note to the column's width; only for Stats given a fixed width.
  property bool elideNote: false

  spacing: Style.space(4)

  Text {
    textFormat: Text.PlainText
    text: root.label
    color: Qt.darker(root.foreground, 1.4)
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
    font.letterSpacing: 1
  }

  Row {
    spacing: Style.space(3)

    Text {
      id: valueText
      textFormat: Text.PlainText
      text: root.value
      color: root.value === "—" ? Qt.darker(root.foreground, 1.6) : root.foreground
      font.family: root.fontFamily
      font.pixelSize: root.valueSize
    }

    Text {
      textFormat: Text.PlainText
      visible: root.unit !== "" && root.value !== "—"
      anchors.baseline: valueText.baseline
      text: root.unit
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  Text {
    width: root.elideNote ? root.width : implicitWidth
    elide: root.elideNote ? Text.ElideRight : Text.ElideNone
    textFormat: Text.PlainText
    visible: text !== ""
    text: root.note
    color: Qt.darker(root.foreground, 1.5)
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
  }
}
