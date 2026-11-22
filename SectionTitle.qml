import QtQuick
import qs.Commons
import qs.Ui

// A section's small-caps title, with an optional detail line on the right.
Item {
  id: root

  property string text: ""
  property string detail: ""
  property color foreground: "white"
  property string fontFamily: Style.font.family

  implicitHeight: Math.max(titleText.implicitHeight, detailText.implicitHeight)

  PanelSectionHeader {
    id: titleText
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    text: root.text
    foreground: root.foreground
    fontFamily: root.fontFamily
    font.letterSpacing: 1.2
  }
  Text {
    id: detailText
    anchors.right: parent.right
    anchors.left: titleText.right
    anchors.leftMargin: Style.space(12)
    anchors.verticalCenter: parent.verticalCenter
    horizontalAlignment: Text.AlignRight
    elide: Text.ElideLeft
    textFormat: Text.PlainText
    text: root.detail
    color: Qt.darker(root.foreground, 1.6)
    font.family: root.fontFamily
    font.pixelSize: Style.font.caption
    font.bold: true
    font.letterSpacing: 1
  }
}
