import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import "Model.js" as Model

// Client of the Helios daemon (helios.service). The daemon owns the Bluetooth
// connection and history; this reads its Unix socket, which sends the full
// state on connect and whenever it changes, plus small heart-rate events.
//
// The shell loads one of these for the plugin (the manifest's "service"
// kind) and every bar's widget shares it, so three monitors mean one socket,
// not three. It also answers the plugin's IPC target: a target registered by
// each monitor's widget reaches only whichever registered first.
Item {
  id: root

  // Injected by the shell.
  property var shell: null
  property var manifest: null
  // The panel compares this with the daemon's version.
  readonly property string pluginVersion: manifest && manifest.version ? String(manifest.version) : ""

  property var snapshot: null
  property var heartRate: null   // { bpm, at } while the strap streams
  readonly property bool daemonRunning: socket.connected
  readonly property string status: !socket.connected ? "no-daemon" : (snapshot ? snapshot.status : "starting")
  readonly property bool connected: status === "connected"
  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || ""
  readonly property string socketPath: runtimeDir !== "" ? runtimeDir + "/helios/control.sock" : ""

  // Whether this instance answers the plugin's IPC target.
  property bool handlesIpc: true
  // The bar widgets sharing this client, one per monitor.
  property var widgets: []

  function attach(widget) {
    if (widgets.indexOf(widget) < 0) widgets = widgets.concat([widget])
  }

  function detach(widget) {
    widgets = widgets.filter(function(w) { return w !== widget })
  }

  // The widget on the monitor Hyprland has focused: where a panel summoned
  // from the keyboard belongs.
  function focusedWidget() {
    var monitor = Hyprland.focusedMonitor
    var name = monitor ? String(monitor.name || "") : ""
    for (var i = 0; i < widgets.length; i++)
      if (name !== "" && widgets[i].screenName === name) return widgets[i]
    for (var o = 0; o < widgets.length; o++) if (widgets[o].opened) return widgets[o]
    return widgets.length ? widgets[0] : null
  }

  function sync() {
    send({ cmd: "sync" })
  }

  function send(command) {
    if (!socket.connected) return
    socket.write(JSON.stringify(command) + "\n")
    socket.flush()
  }

  // Try the daemon now rather than at the next retry, for a panel that has
  // just been opened on "service is not running".
  function reconnect() {
    if (socket.connected || socketPath === "") return
    retry.interval = retry.shortest
    socket.connected = false
    socket.connected = true
  }

  // { bpm, at } from a message, if it carries a rate a heart can beat at.
  function live(reading) {
    var bpm = reading ? Model.pulse(reading.bpm) : null
    return bpm === null ? null : { bpm: bpm, at: reading.at }
  }

  function handle(line) {
    var message
    try {
      message = JSON.parse(line)
    } catch (e) {
      return
    }
    if (!message || typeof message !== "object") return
    if (message.type === "state") {
      root.snapshot = message
      root.heartRate = root.live(message.heartRate)
    } else if (message.type === "hr") {
      root.heartRate = root.live(message)
    }
  }

  Socket {
    id: socket
    path: root.socketPath
    connected: true
    parser: SplitParser {
      onRead: function(line) { root.handle(line) }
    }
    onConnectionStateChanged: {
      if (connected) {
        retry.interval = retry.shortest
        // The daemon sends its state on connect, but if the last connection
        // ended mid-line that first line may be read as the end of the old
        // one and dropped. Asking again costs one message.
        root.send({ cmd: "state" })
      } else {
        root.heartRate = null
      }
    }
  }

  // The daemon may start after the shell, or restart; reconnect quietly.
  // Each failed try is a line in the shell's log, so they thin out while
  // the daemon stays away (it isn't set up, or was stopped on purpose).
  Timer {
    id: retry
    readonly property int shortest: 5000
    readonly property int longest: 60000
    interval: shortest
    repeat: true
    running: !socket.connected && root.socketPath !== ""
    onTriggered: {
      interval = Math.min(longest, interval * 2)
      socket.connected = false
      socket.connected = true
    }
  }

  Loader {
    active: root.handlesIpc
    sourceComponent: IpcHandler {
      target: "atlastimed.helios"

      function open(): void { var w = root.focusedWidget(); if (w) w.open() }
      function close(): void { for (var i = 0; i < root.widgets.length; i++) root.widgets[i].close() }
      function show(): void { open() }
      function hide(): void { close() }
      function toggle(): void { var w = root.focusedWidget(); if (w) w.togglePanel() }
      function sync(): void { root.sync() }
    }
  }
}
