import QtQuick
import qs.Commons
import "Model.js" as Model

// Time spent in each zone as one segmented bar, with the zone names and
// durations underneath. Empty zones keep their label but take no width.
// The labels take the room their text needs and wrap when a long duration
// leaves too little, rather than each holding a fixed share of the width.
Column {
  id: root

  property var zones: [0, 0, 0, 0]     // minutes per zone
  property var labels: []
  property var colors: []
  property color foreground: "white"
  property string fontFamily: Style.font.family
  property bool showLabels: true

  readonly property real total: zones.reduce(function(a, b) { return a + b }, 0)

  spacing: Style.space(6)

  Row {
    id: bar
    width: parent.width
    height: Style.space(6)
    spacing: root.total > 0 ? Style.space(2) : 0

    Rectangle {
      visible: root.total === 0
      width: parent.width
      height: parent.height
      radius: Math.min(Style.cornerRadius, height / 2)
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
    }

    Repeater {
      model: root.total > 0 ? root.zones.length : 0
      Rectangle {
        required property int index
        readonly property int shown: root.zones.filter(function(z) { return z > 0 }).length
        visible: root.zones[index] > 0
        width: Math.max(2, (bar.width - bar.spacing * Math.max(0, shown - 1)) * root.zones[index] / root.total)
        height: bar.height
        radius: Math.min(Style.cornerRadius, height / 2)
        color: root.colors[index]
      }
    }
  }

  Flow {
    visible: root.showLabels
    width: parent.width
    spacing: Style.space(14)
    Repeater {
      model: root.labels
      Row {
        required property string modelData
        required property int index
        spacing: Style.space(5)
        Rectangle {
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(6)
          height: Style.space(6)
          radius: Math.min(Style.cornerRadius, width / 2)
          color: root.colors[index]
        }
        Text {
          textFormat: Text.PlainText
          text: modelData
          color: Qt.darker(root.foreground, 1.6)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }
        Text {
          textFormat: Text.PlainText
          text: Model.duration(root.zones[index])
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
