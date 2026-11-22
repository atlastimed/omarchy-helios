import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Heart and live pulse in the bar; the host for the Helios detail panel.
//
// Left click opens the panel, middle click asks the daemon to sync now.
// The daemon (helios.service) does all Bluetooth work; this only listens,
// through the plugin's one Service, which every monitor's widget shares.
BarWidget {
  id: root
  moduleName: "atlastimed.helios"

  // The shared daemon client, from the shell; `ownService` stands in on a
  // shell that didn't load one (the plugin was enabled before it had a
  // service, and the shell hasn't been restarted since).
  property var sharedService: null
  readonly property var service: sharedService || ownService.item
  readonly property string serviceStatus: service ? service.status : "starting"
  readonly property string screenName: QsWindow.window && QsWindow.window.screen ? String(QsWindow.window.screen.name || "") : ""

  readonly property bool showHeartRate: setting("showHeartRate", true) !== false
  readonly property bool pulse: setting("pulse", false) === true
  readonly property bool hideWhenAway: setting("hideWhenAway", false) === true

  readonly property var heartRate: service ? service.heartRate : null
  readonly property var battery: service && service.snapshot && service.snapshot.battery ? service.snapshot.battery : ({})
  readonly property bool needsAttention: Model.needsSetup(serviceStatus)
  readonly property bool lowBattery: battery.level !== undefined && battery.level < 10 && !battery.charging
  // Idle (connect-to-sync mode between syncs) is healthy, and so is the
  // connect before each sync; only a strap that went missing is away.
  property bool lost: false
  readonly property bool away: serviceStatus === "offline" || (lost && serviceStatus === "connecting")
  readonly property string status: serviceStatus
  onStatusChanged: {
    if (status === "offline") lost = true
    else if (status === "connected" || status === "idle") lost = false
  }
  readonly property string bpmText: !vertical && showHeartRate && heartRate ? String(heartRate.bpm) : ""
  readonly property color glyphColor: needsAttention || lowBattery ? (bar ? bar.urgent : Color.urgent) : button.foreground

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  readonly property real openPanelIndicatorWidth: content.width

  function open() {
    // Opened on "service is not running": look for the daemon now.
    if (service && !service.daemonRunning) service.reconnect()
    if (panelLoader.item) panelLoader.item.open()
  }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() {
    if (opened) close()
    else open()
  }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }
  function sync() { if (service) service.sync() }

  function resolveService() {
    if (sharedService) return
    var api = bar ? bar.shell : null
    var found = api && typeof api.serviceFor === "function" ? api.serviceFor("atlastimed.helios") : null
    if (found) sharedService = found
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.settings = root.settings
    target.anchorItem = button
    target.hostWidget = root
    target.service = service
    target.pluginVersion = service && service.pluginVersion ? service.pluginVersion : ""
  }

  visible: !(hideWhenAway && away && !opened)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: {
    resolveService()
    injectPanel()
  }
  onSettingsChanged: injectPanel()
  onServiceChanged: {
    if (service) service.attach(root)
    injectPanel()
  }
  Component.onDestruction: if (service) service.detach(root)

  // The shell registers services as it loads them, which can be after this
  // widget gets its bar: look again for a moment before making a client
  // here, and once a minute after that, in case the shared one turns up.
  Timer {
    id: serviceWait
    property int tries: 0
    interval: tries < 12 ? 250 : 60000
    repeat: true
    running: root.sharedService === null
    onTriggered: {
      root.resolveService()
      if (root.sharedService !== null) ownService.active = false
      else if (++tries >= 12) ownService.active = true
    }
  }

  Loader {
    id: ownService
    active: false
    source: Qt.resolvedUrl("Service.qml")
  }

  Connections {
    target: root.service
    ignoreUnknownSignals: true
    function onPluginVersionChanged() { root.injectPanel() }
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("HeliosPanel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    dimmed: root.away && !root.needsAttention
    tooltipText: ""
    fixedWidth: root.vertical ? -1 : (root.bpmText !== "" ? Math.ceil(content.width + scaledHorizontalMargin * 2) : Style.bar.iconSlot)
    fixedHeight: root.vertical ? Style.bar.iconSlot : -1

    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.sync()
      else if (b === Qt.LeftButton) root.togglePanel()
    }

    Row {
      id: content
      anchors.centerIn: parent
      spacing: root.bpmText !== "" ? Style.space(4) : 0
      scale: button.pressScale !== undefined ? button.pressScale : 1

      // The bar's own icon canvas: the heart is optically centred and sized
      // like the icons beside it, as WidgetButton draws a lone icon.
      BarGlyph {
        id: heart
        width: Style.bar.iconCanvas
        height: Style.bar.iconCanvas
        text: root.needsAttention ? Model.GLYPH_HEART_OFF : root.away || !root.heartRate ? Model.GLYPH_HEART_OUTLINE : Model.GLYPH_HEART
        color: root.glyphColor
        fontFamily: button.fontFamily
        fontSize: Style.bar.iconFont

        Behavior on color {
          enabled: !root.bar || root.bar.foregroundAnimationEnabled
          ColorAnimation { duration: 160 }
        }

        SequentialAnimation {
          id: beat
          NumberAnimation { target: heart; property: "scale"; to: 1.22; duration: 90; easing.type: Easing.OutQuad }
          NumberAnimation { target: heart; property: "scale"; to: 1.0; duration: 180; easing.type: Easing.InOutQuad }
        }
      }

      Item {
        visible: root.bpmText !== ""
        width: bpm.implicitWidth
        height: Style.bar.iconCanvas

        Text {
          id: bpm
          textFormat: Text.PlainText
          text: root.bpmText
          color: button.foreground
          font.family: button.fontFamily
          font.pixelSize: button.fontSize
          renderType: Text.NativeRendering
          // Centre the figures, not the line box, as the bar's labels do.
          y: (parent.height - height) / 2 + (bpmMetrics.descent - bpmMetrics.ascent + bpmMetrics.tightBoundingRect("0").height) / 2
        }

        FontMetrics { id: bpmMetrics; font: bpm.font }
      }
    }
  }

  // The interval is set after each beat rather than bound: changing a running
  // Timer's interval restarts it, and the pulse changes every few seconds.
  Timer {
    running: root.pulse && root.heartRate !== null && root.visible && Style.reduceMotion !== true
    repeat: true
    interval: 1000
    onTriggered: {
      beat.restart()
      if (root.heartRate) interval = Math.round(60000 / Math.max(30, Math.min(230, root.heartRate.bpm)))
    }
  }
}
