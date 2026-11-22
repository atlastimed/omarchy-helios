import QtQuick
import QtQuick.Controls
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// The Helios detail panel: the live pulse and recovery readings over the day's
// heart rate, the last night's sleep beside the wearer's usual, today's load,
// the week's workouts and a week or a month of trends.
//
// Composition follows the weather panel (oversized read-out beside small-caps
// stats) under a PanelHero, with PanelSeparator between sections. Each
// section shows only when it has data of its own.
// BarWidget.qml hands in the daemon client (Service.qml) as `service`.
Panel {
  id: root
  moduleName: "atlastimed.helios"
  ipcTarget: "atlastimed.helios"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property var service: null
  property string pluginVersion: ""
  readonly property var barIdentity: hostWidget || root

  readonly property var snapshot: service ? service.snapshot : null
  readonly property string status: service ? service.status : "starting"
  readonly property var live: service ? service.heartRate : null
  readonly property var today: snapshot && snapshot.today ? snapshot.today : ({})
  readonly property var latest: snapshot && snapshot.latest ? snapshot.latest : ({})
  readonly property var day: snapshot && snapshot.day ? snapshot.day : null
  readonly property var sleep: snapshot && snapshot.sleep ? snapshot.sleep : null
  readonly property var week: snapshot && snapshot.week ? snapshot.week : null
  readonly property var month: snapshot && snapshot.month ? snapshot.month : null
  readonly property var naps: snapshot && snapshot.naps ? snapshot.naps : []
  readonly property var recovery: snapshot && snapshot.recovery ? snapshot.recovery : ({})
  readonly property var workouts: snapshot && snapshot.workouts && snapshot.workouts.recent ? snapshot.workouts.recent : []
  readonly property var battery: snapshot && snapshot.battery ? snapshot.battery : ({})
  readonly property var device: snapshot && snapshot.device ? snapshot.device : ({})

  readonly property bool setupNeeded: Model.needsSetup(status)
  readonly property bool outdated: !!(pluginVersion && snapshot && snapshot.version && snapshot.version !== pluginVersion)
  readonly property bool hasHeartDay: !!(day && day.heartRate && day.heartRate.length > 0)
  readonly property var heartNow: live ? { value: live.bpm, at: live.at, live: true }
    : (latest.heartRate ? { value: latest.heartRate.value, at: latest.heartRate.at, live: false } : null)
  readonly property bool hasReadings: !!(heartNow || latest.hrv || latest.stress || latest.spo2 || latest.temperature)
  // The trends show a week or, from a daemon that sends one, a month.
  property int trendDays: 7
  readonly property var trend: trendDays > 7 && month ? month : week
  readonly property bool monthly: trend !== null && trend === month
  readonly property var restingSlots: trendSlots("restingHr")
  readonly property var hrvSlots: trendSlots("hrv")
  readonly property var sleepSlots: trendSlots("sleep")
  readonly property var stepSlots: trendSlots("steps")
  readonly property bool hasWeek: Model.hasValues(restingSlots) || Model.hasValues(hrvSlots) || Model.hasValues(sleepSlots)
    || Model.hasValues(stepSlots)
  readonly property bool hasCalories: today.calories !== null && today.calories !== undefined
  readonly property bool hasToday: !!(today.pai || today.stress !== null && today.stress !== undefined || today.steps > 0
    || today.calories > 0)

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color accent: Commons.Color.accent
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color faint: Qt.darker(foreground, 1.6)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  // Lane position separates the stages; colour only reinforces it. Urgent is
  // kept for errors, so waking is drawn in the plain foreground.
  readonly property var stageColors: ({
    awake: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.6),
    rem: Qt.lighter(accent, 1.45),
    light: Qt.rgba(accent.r, accent.g, accent.b, 0.6),
    deep: accent
  })
  readonly property bool fahrenheit: Model.useFahrenheit(setting("temperatureUnit", "auto"),
    Qt.locale().measurementSystem === Locale.ImperialUSSystem, snapshot ? snapshot.timezone : "")
  readonly property string timeFormat: Qt.locale().timeFormat(Locale.ShortFormat)

  // Relative times ("synced 4 min ago") only need to move while visible.
  property double now: Date.now()
  // Set by the first key press, so Enter's target is highlighted only for
  // keyboard users, as in the first-party panels.
  property bool keyboardActive: false

  readonly property string heroMeta: Model.statusLine(status, snapshot, now)
  readonly property string notice: {
    if (outdated) return "This widget and the Helios service are different versions. Run setup to bring them in line."
    switch (status) {
    case "no-daemon": return "Run setup to install and start it."
    case "needs-auth": return "Helios needs your strap's key from your Zepp account."
    case "auth-failed": return "The strap rejected its key. If you paired it again in Zepp, fetch the new one."
    case "offline": return snapshot && snapshot.message ? snapshot.message : ""
    }
    return ""
  }
  readonly property string pluginDir: decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "")).replace(/\/$/, "")

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function clock(ts) {
    return Model.clock(ts, function(d) { return Qt.formatTime(d, root.timeFormat) })
  }

  function open() {
    now = Date.now()
    root.controller.show()
  }

  function close() {
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function sync() {
    if (service) service.sync()
  }

  // Setup is interactive (it may ask for the Zepp login), so it runs in
  // Omarchy's floating terminal, where the user sees every step.
  function runSetup() {
    var args = status === "needs-auth" || status === "auth-failed" ? " --auth" : ""
    // The launcher hands its argument to a shell, so the path goes in as one quoted word.
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", Model.shellQuote(pluginDir + "/setup") + args])
    close()
  }

  function primaryAction() {
    if (setupNeeded || outdated) runSetup()
    else sync()
  }

  function scrollBy(dy) {
    var limit = Math.max(0, flick.contentHeight - flick.height)
    flick.contentY = Math.max(0, Math.min(limit, flick.contentY + dy))
  }

  function trendSlots(key) {
    return Model.daySlots(trend ? trend[key] : [], trend ? trend.start : 0, trend && trend.days ? trend.days : 7)
  }

  function trendNote(slots, digits) {
    var average = Model.slotAverage(slots)
    return monthly && average !== null ? "avg " + Model.number(average, digits) : ""
  }

  function toggleTrend() {
    if (month) trendDays = trendDays > 7 ? 7 : 30
  }

  onOpenedChanged: if (opened) {
    keyboardActive = false
    if (flick) flick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.opened
    onTriggered: root.now = Date.now()
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(440))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onMoveRequested: function(dx, dy) {
        root.keyboardActive = true
        if (dy !== 0) root.scrollBy(dy * Style.space(56))
      }
      onActivateRequested: {
        root.keyboardActive = true
        root.primaryAction()
      }
      onTextKey: function(t) {
        root.keyboardActive = true
        if (t === "s" || t === "S" || t === "r" || t === "R") root.sync()
        else if (t === "t" || t === "T") root.toggleTrend()
      }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width
          spacing: Style.space(14)

          // ---- Hero: the strap, its connection, its battery.
          PanelHero {
            width: parent.width
            title: "Helio Strap"
            meta: root.heroMeta
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.status === "connected" || root.setupNeeded ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: Model.GLYPH_STRAP
                color: root.setupNeeded ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              Row {
                visible: root.battery.level !== undefined
                spacing: Style.space(5)

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: Model.batteryGlyph(root.battery.level, root.battery.charging)
                  color: root.battery.level < 15 && !root.battery.charging ? root.urgent : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.icon
                }
                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: root.battery.level !== undefined ? root.battery.level + "%" : ""
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                }
              }
            }
          }

          // ---- Setup, errors and connection notes.
          Item {
            visible: root.notice !== ""
            width: parent.width
            height: Math.max(noticeText.implicitHeight, setupButton.visible ? setupButton.implicitHeight : 0)

            Text {
              id: noticeText
              anchors.left: parent.left
              anchors.right: setupButton.visible ? setupButton.left : parent.right
              anchors.rightMargin: setupButton.visible ? Style.space(12) : 0
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: root.notice
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Button {
              id: setupButton
              visible: root.setupNeeded || root.outdated
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.status === "needs-auth" || root.status === "auth-failed" ? "Fetch key" : "Set up Helios"
              iconText: Model.GLYPH_SETUP
              bordered: true
              focusable: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              tooltipText: "Opens setup in a terminal (Enter)"
              hasCursor: root.keyboardActive
              onClicked: root.runSetup()
            }
          }

          Text {
            visible: !root.setupNeeded && !root.hasReadings && root.snapshot !== null
            width: parent.width
            textFormat: Text.PlainText
            text: root.status === "connected" ? "No readings yet. They appear after the first sync." : "No readings yet."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          // ---- Now: the pulse, oversized, beside the recovery readings.
          Item {
            visible: root.hasReadings
            width: parent.width
            height: Math.max(pulseBlock.height, stats.height)

            Column {
              id: pulseBlock
              anchors.left: parent.left
              anchors.leftMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Row {
                spacing: Style.space(6)

                Text {
                  id: bigBpm
                  textFormat: Text.PlainText
                  text: root.heartNow ? String(Math.round(root.heartNow.value)) : "—"
                  color: root.heartNow ? root.foreground : root.dim
                  font.family: root.fontFamily
                  // Hero read-out, deliberately outside the Style.font scale
                  // like the weather panel's temperature.
                  font.pixelSize: Style.space(52)
                  font.bold: true
                }
                Column {
                  anchors.top: bigBpm.top
                  anchors.topMargin: Style.space(9)
                  spacing: Style.space(2)

                  Text {
                    id: liveHeart
                    textFormat: Text.PlainText
                    text: Model.GLYPH_HEART
                    color: root.heartNow && root.heartNow.live ? root.accent : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.heading

                    // The panel is transient, so it can afford to beat.
                    SequentialAnimation {
                      id: liveBeat
                      NumberAnimation { target: liveHeart; property: "scale"; to: 1.25; duration: 90; easing.type: Easing.OutQuad }
                      NumberAnimation { target: liveHeart; property: "scale"; to: 1.0; duration: 200; easing.type: Easing.InOutQuad }
                    }
                    // The interval is set after each beat rather than bound:
                    // changing a running Timer's interval restarts it.
                    Timer {
                      running: root.opened && root.live !== null && Style.reduceMotion !== true
                      repeat: true
                      interval: 1000
                      onTriggered: {
                        liveBeat.restart()
                        if (root.live) interval = Math.round(60000 / Math.max(30, Math.min(230, root.live.bpm)))
                      }
                    }
                  }
                  Text {
                    textFormat: Text.PlainText
                    text: "BPM"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                    font.letterSpacing: 1.2
                  }
                }
              }

              Text {
                textFormat: Text.PlainText
                text: !root.heartNow ? "NO RECENT READING"
                  : root.heartNow.live ? "LIVE"
                  : "AT " + root.clock(root.heartNow.at).toUpperCase()
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.2
              }
            }

            Grid {
              id: stats
              anchors.right: parent.right
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              columns: 2
              columnSpacing: Style.space(30)
              rowSpacing: Style.space(12)

              Stat {
                label: "HRV"
                value: Model.reading(root.latest.hrv)
                unit: "ms"
                note: Model.staleNote(root.latest.hrv, root.now)
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                label: "STRESS"
                value: Model.reading(root.latest.stress)
                note: root.latest.stress ? Model.joinNotes(Model.stressLabel(root.latest.stress.value), Model.staleNote(root.latest.stress, root.now)) : ""
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                label: "SPO2"
                value: Model.reading(root.latest.spo2)
                unit: "%"
                note: Model.staleNote(root.latest.spo2, root.now)
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                label: "SKIN"
                value: root.latest.temperature ? Model.temperature(root.latest.temperature.value, root.fahrenheit) : "—"
                unit: root.fahrenheit ? "°F" : "°C"
                note: Model.staleNote(root.latest.temperature, root.now)
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
            }
          }

          // ---- Heart rate across the last 24 hours.
          PanelSeparator { visible: root.hasHeartDay; foreground: root.foreground }

          Column {
            visible: root.hasHeartDay
            width: parent.width
            spacing: Style.space(8)

            SectionTitle {
              foreground: root.foreground
              fontFamily: root.fontFamily
              width: parent.width
              text: "HEART RATE · 24 H"
              // The dashes match the chart's resting-rate rule.
              detail: root.day && root.day.heartRateStats
                ? Model.number(root.day.heartRateStats.min) + "–" + Model.number(root.day.heartRateStats.max) + " · AVG " + Model.number(root.day.heartRateStats.avg)
                  + (root.today.restingHr ? " · ┄ RESTING " + Model.number(root.today.restingHr.value) : "")
                : ""
            }

            LineChart {
              id: hrChart
              width: parent.width
              height: Style.space(72)
              points: root.day ? root.day.heartRate : []
              start: root.day ? root.day.start : 0
              end: root.day ? root.day.end : 1
              bucket: 600
              lineColor: root.accent
              bandColor: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.18)
              gridColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)
              markerColor: root.foreground
              guides: root.today.restingHr ? [root.today.restingHr.value] : []

              // The rate under the pointer, beside it.
              ChartTip {
                readonly property var tip: Model.heartTip(hrChart.hoveredPoint, function(d) { return Qt.formatTime(d, root.timeFormat) })
                shown: hrChart.hoveredPoint !== null
                title: tip.title
                detail: tip.detail
                pointerX: hrChart.pointerX
                pointerY: hrChart.pointerY
                fontFamily: root.fontFamily
              }
            }

            Item {
              width: parent.width
              height: nowLabel.implicitHeight

              Repeater {
                model: root.day ? Model.dayTicks(root.day.start, root.day.end, function(d) { return Model.hourLabel(Qt.formatTime(d, root.timeFormat)) }) : []
                Text {
                  required property var modelData
                  x: Math.max(0, Math.min(parent.width - width, hrChart.xFor(modelData.at) - width / 2))
                  // Drop a tick that would run into the NOW label.
                  visible: x + width < nowLabel.x - Style.space(16)
                  textFormat: Text.PlainText
                  text: modelData.label
                  color: root.faint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
              Text {
                id: nowLabel
                anchors.right: parent.right
                textFormat: Text.PlainText
                text: "NOW"
                color: root.faint
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }
            }
          }

          // ---- The last sleep.
          PanelSeparator { visible: root.sleep !== null; foreground: root.foreground }

          Column {
            visible: root.sleep !== null
            width: parent.width
            spacing: Style.space(10)

            SectionTitle {
              foreground: root.foreground
              fontFamily: root.fontFamily
              width: parent.width
              text: "SLEEP"
              detail: root.sleep
                ? (Model.weekday(root.sleep.start) + " " + root.clock(root.sleep.start) + " → "
                   + (Model.weekday(root.sleep.end) !== Model.weekday(root.sleep.start) ? Model.weekday(root.sleep.end) + " " : "")
                   + root.clock(root.sleep.end)).toUpperCase()
                : ""
            }

            Item {
              width: parent.width
              height: sleepTotal.height

              Column {
                id: sleepTotal
                spacing: Style.space(2)

                Row {
                  spacing: Style.space(8)
                  Text {
                    id: sleepDuration
                    textFormat: Text.PlainText
                    text: root.sleep ? Model.duration(root.sleep.asleepMinutes) : ""
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.display
                    font.bold: true
                  }
                  Text {
                    anchors.baseline: sleepDuration.baseline
                    textFormat: Text.PlainText
                    text: "asleep"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }
                }
                // A sleep far longer than a night probably has a nap merged in.
                Text {
                  visible: text !== ""
                  textFormat: Text.PlainText
                  text: !root.sleep ? ""
                    : root.sleep.asleepMinutes > 12 * 60 ? "Longer than a night; may include a nap"
                    : root.sleep.fellAsleepMinutes > 0 ? "Fell asleep in " + Model.duration(root.sleep.fellAsleepMinutes)
                    : ""
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(6)
                visible: root.sleep && root.sleep.score > 0

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: root.sleep ? Model.sleepScoreLabel(root.sleep.score).toUpperCase() : ""
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  font.letterSpacing: 1.2
                }
                BorderSurface {
                  implicitWidth: scoreText.implicitWidth + Style.space(12)
                  implicitHeight: scoreText.implicitHeight + Style.space(4)
                  color: "transparent"
                  borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
                  radius: Style.cornerRadius
                  Text {
                    id: scoreText
                    anchors.centerIn: parent
                    textFormat: Text.PlainText
                    text: root.sleep ? "SCORE " + root.sleep.score : ""
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                  }
                }
              }
            }

            // Lane labels carry each stage's total, so the chart needs no legend.
            Row {
              width: parent.width
              spacing: Style.space(10)

              Column {
                id: laneLabels
                Repeater {
                  model: [
                    { key: "awake", label: "AWAKE" },
                    { key: "rem", label: "REM" },
                    { key: "light", label: "LIGHT" },
                    { key: "deep", label: "DEEP" }
                  ]
                  Row {
                    required property var modelData
                    height: hypnogram.laneHeight
                    spacing: Style.space(6)

                    Rectangle {
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(6)
                      height: Style.space(6)
                      radius: Math.min(Style.cornerRadius, width / 2)
                      color: root.stageColors[modelData.key]
                    }
                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      width: laneNameProbe.implicitWidth
                      textFormat: Text.PlainText
                      text: modelData.label
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }
                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      width: laneValueProbe.implicitWidth
                      horizontalAlignment: Text.AlignRight
                      textFormat: Text.PlainText
                      text: root.sleep ? Model.duration(root.sleep.minutes[modelData.key]) : ""
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
                Text { id: laneNameProbe; visible: false; text: "AWAKE"; font.family: root.fontFamily; font.pixelSize: Style.font.caption; font.bold: true }
                Text { id: laneValueProbe; visible: false; text: "10h 00m"; font.family: root.fontFamily; font.pixelSize: Style.font.caption }
              }

              Hypnogram {
                id: hypnogram
                width: parent.width - laneLabels.width - parent.spacing
                height: Style.space(72)
                stages: root.sleep ? root.sleep.stages : []
                start: root.sleep ? root.sleep.start : 0
                end: root.sleep ? root.sleep.end : 1
                colors: root.stageColors
                riserColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.2)
                markerColor: root.foreground
                unstagedColor: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
                radius: Math.max(1.5, Style.cornerRadius)

                ChartTip {
                  readonly property var tip: Model.stageTip(hypnogram.hoveredStage, function(d) { return Qt.formatTime(d, root.timeFormat) })
                  shown: hypnogram.hoveredStage !== null
                  title: tip.title
                  detail: tip.detail
                  pointerX: hypnogram.pointerX
                  pointerY: hypnogram.pointerY
                  fontFamily: root.fontFamily
                }
              }
            }

            Text {
              visible: root.sleep !== null && Model.hasUnstaged(root.sleep.stages)
              width: parent.width
              textFormat: Text.PlainText
              text: "The strap keeps 50 stages a night. The rest of this one is the pale band; the totals still count it."
              color: root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            // The night's readings, each beside the wearer's usual: the
            // average of the nights before it.
            Grid {
              id: nightStats
              width: parent.width
              columns: 3
              columnSpacing: Style.space(4)
              rowSpacing: Style.space(10)
              readonly property real cell: (width - columnSpacing * 2) / 3

              Stat {
                width: nightStats.cell
                elideNote: true
                label: "AVG HR"
                value: root.sleep && root.sleep.avgHr ? String(root.sleep.avgHr) : "—"
                unit: "bpm"
                note: root.sleep && root.sleep.hrMin ? "Low " + Model.number(root.sleep.hrMin) : ""
                valueSize: Style.font.body
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                width: nightStats.cell
                elideNote: true
                label: "HRV"
                value: root.sleep ? Model.number(root.sleep.hrv) : "—"
                unit: "ms"
                note: Model.usualNote(root.recovery.hrv)
                valueSize: Style.font.body
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                width: nightStats.cell
                elideNote: true
                label: "BREATHING"
                value: root.sleep ? Model.number(root.sleep.respiration, 1) : "—"
                unit: "/min"
                note: Model.usualNote(root.recovery.respiration, 1)
                valueSize: Style.font.body
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                width: nightStats.cell
                elideNote: true
                label: "RESTING HR"
                value: root.recovery.restingHr ? Model.number(root.recovery.restingHr.value) : "—"
                unit: "bpm"
                note: Model.usualNote(root.recovery.restingHr)
                valueSize: Style.font.body
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                width: nightStats.cell
                elideNote: true
                label: "SKIN TEMP"
                value: root.recovery.temperature ? Model.temperature(root.recovery.temperature.value, root.fahrenheit) : "—"
                unit: root.fahrenheit ? "°F" : "°C"
                note: Model.temperatureNote(root.recovery.temperature, root.fahrenheit)
                valueSize: Style.font.body
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                width: nightStats.cell
                elideNote: true
                label: "SPO2"
                value: root.sleep ? Model.number(root.sleep.spo2) : "—"
                unit: "%"
                note: root.sleep && root.sleep.spo2Min ? "Low " + Model.number(root.sleep.spo2Min) + "%" : ""
                valueSize: Style.font.body
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
            }

            Text {
              visible: root.recovery.nights !== undefined
              width: parent.width
              textFormat: Text.PlainText
              text: root.recovery.nights >= 7
                ? "Usual is your average over earlier nights (" + root.recovery.nights + " so far)."
                : "Your usual appears after 7 nights; " + (root.recovery.nights || 0) + " so far."
              color: root.faint
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            // Naps since that night, as the strap filed them.
            Repeater {
              model: root.naps
              Item {
                required property var modelData
                width: parent.width
                height: napLabel.implicitHeight

                Text {
                  id: napLabel
                  textFormat: Text.PlainText
                  text: "NAP"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  font.letterSpacing: 1
                }
                Text {
                  anchors.right: parent.right
                  anchors.baseline: napLabel.baseline
                  textFormat: Text.PlainText
                  text: Model.napLine(modelData, function(d) { return Qt.formatTime(d, root.timeFormat) })
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }
              }
            }
          }

          // ---- Today's activity and load.
          PanelSeparator { visible: root.hasToday; foreground: root.foreground }

          Column {
            visible: root.hasToday
            width: parent.width
            spacing: Style.space(10)

            SectionTitle {
              foreground: root.foreground
              fontFamily: root.fontFamily
              width: parent.width
              text: "TODAY"
            }

            Row {
              id: todayStats
              width: parent.width
              spacing: Style.space(4)
              readonly property int shown: root.hasCalories ? 4 : 3
              readonly property real cell: (width - spacing * (shown - 1)) / shown

              Stat {
                width: todayStats.cell
                elideNote: true
                label: "STEPS"
                value: Model.grouped(root.today.steps)
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                visible: root.hasCalories
                width: todayStats.cell
                elideNote: true
                label: "ACTIVE"
                value: Model.grouped(root.today.calories)
                unit: "kcal"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                width: todayStats.cell
                elideNote: true
                label: root.hasCalories ? "PAI" : "PAI · GOAL 100"
                value: root.today.pai ? Model.number(root.today.pai.total, root.today.pai.total < 10 ? 1 : 0) : "—"
                note: Model.paiNote(root.today.pai)
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
              Stat {
                width: todayStats.cell
                elideNote: true
                label: "STRESS AVG"
                value: Model.number(root.today.stress)
                note: Model.stressLabel(root.today.stress)
                foreground: root.foreground
                fontFamily: root.fontFamily
              }
            }

            ZoneBar {
              width: parent.width
              zones: root.today.stressZones || [0, 0, 0, 0]
              labels: ["RELAXED", "NORMAL", "MEDIUM", "HIGH"]
              colors: [
                Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.6),
                root.accent,
                Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.7),
                root.urgent
              ]
              foreground: root.foreground
              fontFamily: root.fontFamily
            }
          }

          // ---- Workouts the strap recorded this week.
          PanelSeparator { visible: root.workouts.length > 0; foreground: root.foreground }

          Column {
            visible: root.workouts.length > 0
            width: parent.width
            spacing: Style.space(10)

            SectionTitle {
              foreground: root.foreground
              fontFamily: root.fontFamily
              width: parent.width
              text: "WORKOUTS · 7 DAYS"
              detail: root.snapshot && root.snapshot.workouts && root.snapshot.workouts.load > 0
                ? "TRAINING LOAD " + root.snapshot.workouts.load : ""
            }

            Repeater {
              model: root.workouts
              Column {
                id: workoutRow
                required property var modelData
                required property int index
                width: parent.width
                spacing: Style.space(4)

                Item {
                  width: parent.width
                  height: workoutName.implicitHeight

                  Text {
                    id: workoutName
                    textFormat: Text.PlainText
                    text: Model.workoutTitle(workoutRow.modelData)
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                  }
                  Text {
                    anchors.right: parent.right
                    anchors.baseline: workoutName.baseline
                    textFormat: Text.PlainText
                    text: Model.workoutWhen(workoutRow.modelData, function(d) { return Qt.formatTime(d, root.timeFormat) })
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                  }
                }
                Text {
                  visible: text !== ""
                  width: parent.width
                  textFormat: Text.PlainText
                  text: Model.workoutFigures(workoutRow.modelData)
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
                // The latest workout also shows where its heart rate sat.
                ZoneBar {
                  visible: workoutRow.index === 0 && total > 0
                  width: parent.width
                  showLabels: false
                  zones: Model.workoutZones(workoutRow.modelData)
                  colors: [
                    Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.35),
                    Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.6),
                    root.accent,
                    Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.7),
                    root.urgent
                  ]
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                }
                Text {
                  visible: workoutRow.index === 0 && text !== ""
                  width: parent.width
                  textFormat: Text.PlainText
                  text: Model.workoutZoneLine(workoutRow.modelData)
                  color: root.faint
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                  elide: Text.ElideRight
                }
              }
            }
          }

          // ---- The week or the month.
          PanelSeparator { visible: root.hasWeek; foreground: root.foreground }

          Column {
            visible: root.hasWeek
            width: parent.width
            spacing: Style.space(10)

            Item {
              width: parent.width
              height: Math.max(trendTitle.implicitHeight, trendSwitch.visible ? trendSwitch.implicitHeight : 0)

              SectionTitle {
                id: trendTitle
                anchors.left: parent.left
                anchors.right: trendSwitch.visible ? trendSwitch.left : parent.right
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                fontFamily: root.fontFamily
                text: root.monthly ? "LAST 30 DAYS" : "LAST 7 DAYS"
              }
              ButtonGroup {
                id: trendSwitch
                visible: root.month !== null
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                options: [
                  { value: "7", label: "7 D", tooltip: "A week of trends (T)" },
                  { value: "30", label: "30 D", tooltip: "A month of trends (T)" }
                ]
                value: root.monthly ? "30" : "7"
                focusable: false
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                onChanged: function(v) { root.trendDays = Number(v) }
              }
            }

            Grid {
              id: trendGrid
              width: parent.width
              columns: 2
              columnSpacing: Style.space(24)
              rowSpacing: Style.space(14)
              readonly property real cell: (width - columnSpacing) / 2
              readonly property string firstLabel: root.trend ? Model.dayLabel(root.trend.start) : ""

              TrendBars {
                width: trendGrid.cell
                label: "RESTING HR"
                unit: "bpm"
                fromZero: false
                slots: root.restingSlots
                note: root.trendNote(root.restingSlots)
                firstLabel: trendGrid.firstLabel
                lastLabel: "TODAY"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
              }
              TrendBars {
                width: trendGrid.cell
                label: "HRV"
                unit: "ms"
                fromZero: false
                partialToday: true
                slots: root.hrvSlots
                note: root.trendNote(root.hrvSlots)
                firstLabel: trendGrid.firstLabel
                lastLabel: "TODAY"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
              }
              TrendBars {
                width: trendGrid.cell
                label: "SLEEP SCORE"
                fromZero: false
                slots: root.sleepSlots
                note: root.trendNote(root.sleepSlots)
                firstLabel: trendGrid.firstLabel
                lastLabel: "TODAY"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
              }
              TrendBars {
                width: trendGrid.cell
                label: "STEPS"
                grouped: true
                partialToday: true
                slots: root.stepSlots
                note: root.trendNote(root.stepSlots)
                firstLabel: trendGrid.firstLabel
                lastLabel: "TODAY"
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
              }
            }
          }

          // ---- The strap itself.
          PanelSeparator { visible: root.device.firmware !== undefined; foreground: root.foreground }

          Item {
            visible: root.device.firmware !== undefined && !root.setupNeeded
            width: parent.width
            height: Math.max(deviceText.implicitHeight, syncButton.implicitHeight)

            Text {
              id: deviceText
              anchors.left: parent.left
              anchors.right: syncButton.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              textFormat: Text.PlainText
              text: [
                root.device.firmware ? "Firmware " + root.device.firmware : "",
                root.battery.lastCharge ? "charged " + Model.ago(root.battery.lastCharge, root.now) : "",
                Model.signalNote(root.snapshot ? root.snapshot.signal : null, root.now)
              ].filter(function(s) { return s !== "" }).join(" · ")
              color: Qt.darker(root.foreground, 1.5)
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              // Room for the signal note beside the button: a second line.
              wrapMode: Text.WordWrap
              maximumLineCount: 2
              elide: Text.ElideRight
            }

            Button {
              id: syncButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.snapshot && root.snapshot.syncing ? "Syncing…" : "Sync now"
              iconText: Model.GLYPH_SYNC
              // Only while it can be seen: a spinner in a closed panel still animates.
              iconSpinning: root.opened && !!(root.snapshot && root.snapshot.syncing)
              enabled: ["connected", "idle", "offline"].indexOf(root.status) !== -1
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              tooltipText: "Fetch new readings from the strap (S)"
              hasCursor: root.keyboardActive && !root.setupNeeded && !root.outdated
              onClicked: root.sync()
            }
          }
        }
      }
    }
  }
}
