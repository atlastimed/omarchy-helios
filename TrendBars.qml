import QtQuick
import qs.Commons
import "Model.js" as Model

// One metric over the last days: a caption, the latest value, and a bar per
// day. A week of bars carries weekday initials; a longer run is drawn dense,
// with only its first and last day named. With partialToday, today's bar is
// outlined because the day isn't over.
//
// Pointing at a bar puts that day's value and date in the headline, so the
// bars can be read exactly as well as compared.
Column {
  id: root

  property string label: ""
  property string unit: ""
  property var slots: []           // { day, label, value }, oldest first
  property string note: ""         // beside the value, e.g. the average
  property string firstLabel: ""   // under a dense run's first and last bars
  property string lastLabel: ""
  property int digits: 0
  property bool grouped: false     // thousands separators, for counts like steps
  // Bars rise from zero, which is honest for counts. A reading that lives in
  // a narrow band (a resting heart rate between 64 and 76) would then draw as
  // a row of equals, so those rise from a floor below the lowest day instead.
  property bool fromZero: true
  property color foreground: "white"
  property color accent: "white"
  property string fontFamily: Style.font.family
  property real barHeight: Style.space(30)
  property bool partialToday: false   // today's value is still accumulating
  readonly property bool dense: slots.length > 7

  readonly property var values: slots.map(function(s) { return s.value }).filter(function(v) { return v !== null })
  readonly property real maxValue: values.length ? Math.max.apply(null, values) : 1
  readonly property real minValue: values.length ? Math.min.apply(null, values) : 0
  // Low enough that the lowest day still stands a quarter of the height.
  readonly property real floorValue: fromZero || maxValue <= minValue ? 0
    : Math.max(0, minValue - (maxValue - minValue) / 3)
  readonly property var latest: {
    for (var i = slots.length - 1; i >= 0; i--) if (slots[i].value !== null) return slots[i].value
    return null
  }

  // The bar under the pointer, or -1.
  property int hovered: -1
  readonly property var hoveredSlot: hovered >= 0 && hovered < slots.length && slots[hovered].value !== null
    ? slots[hovered] : null
  readonly property var shownValue: hoveredSlot ? hoveredSlot.value : latest

  function share(value) {
    if (value === null) return 0
    // Every day the same: full bars, unless the same is nothing at all.
    if (maxValue <= floorValue) return value > 0 ? 1 : 0
    return Math.max(0, (value - floorValue) / (maxValue - floorValue))
  }

  onSlotsChanged: hovered = -1

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
      id: headline
      textFormat: Text.PlainText
      text: root.shownValue === null ? "—"
        : root.grouped ? Model.grouped(root.shownValue)
        : Number(root.shownValue).toFixed(root.digits)
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.title
      font.bold: true
    }
    Text {
      textFormat: Text.PlainText
      visible: root.shownValue !== null && root.unit !== ""
      anchors.baseline: headline.baseline
      text: root.unit
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
    Text {
      textFormat: Text.PlainText
      visible: text !== ""
      anchors.baseline: headline.baseline
      leftPadding: Style.space(5)
      // The day being pointed at, otherwise the caller's note.
      text: root.hoveredSlot ? Model.weekday(root.hoveredSlot.day) + " " + Model.dayLabel(root.hoveredSlot.day) : root.note
      color: root.hoveredSlot ? root.foreground : Qt.darker(root.foreground, 1.6)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  Item {
    width: bars.width
    height: bars.height

    Row {
      id: bars
      spacing: root.dense ? 1 : Style.space(3)
      readonly property int count: Math.max(1, root.slots.length)
      readonly property real barWidth: Math.max(root.dense ? 1 : Style.space(6),
        Math.floor((root.width - spacing * (count - 1)) / count))

      Repeater {
        model: root.slots

        Column {
          required property var modelData
          required property int index
          spacing: Style.space(2)

          Item {
            width: bars.barWidth
            height: root.barHeight

            Rectangle {
              anchors.bottom: parent.bottom
              width: parent.width
              height: modelData.value === null ? Style.spacing.hairline
                : Math.max(2, parent.height * root.share(modelData.value))
              radius: Math.min(Style.cornerRadius, width / 2)
              // Today is still in progress: an accent outline rather than a solid bar.
              readonly property bool partial: root.partialToday && index === root.slots.length - 1
              readonly property bool pointed: index === root.hovered && modelData.value !== null
              color: modelData.value === null ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.15)
                : pointed ? root.accent
                : partial ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.35)
                : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.35)
              border.width: partial && modelData.value !== null ? Style.spacing.hairline : 0
              border.color: root.accent
            }
          }
          Text {
            visible: !root.dense
            width: bars.barWidth
            horizontalAlignment: Text.AlignHCenter
            textFormat: Text.PlainText
            text: modelData.label
            color: Qt.darker(root.foreground, index === root.slots.length - 1 || index === root.hovered ? 1.0 : 1.6)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }

    MouseArea {
      anchors.fill: parent
      hoverEnabled: true
      acceptedButtons: Qt.NoButton
      onPositionChanged: function(mouse) {
        var index = Math.floor(mouse.x / (bars.barWidth + bars.spacing))
        root.hovered = index >= 0 && index < root.slots.length ? index : -1
      }
      onExited: root.hovered = -1
    }
  }

  Item {
    visible: root.dense
    width: bars.width
    height: firstDay.implicitHeight

    Text {
      id: firstDay
      textFormat: Text.PlainText
      text: root.firstLabel
      color: Qt.darker(root.foreground, 1.6)
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
    Text {
      anchors.right: parent.right
      textFormat: Text.PlainText
      text: root.lastLabel
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}
