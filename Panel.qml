import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "CockpitModel.js" as Model

Panel {
  id: root
  moduleName: "reidenxerx.cockpit"
  ipcTarget: "reidenxerx.cockpit"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property string pluginBin: decodeURIComponent(String(Qt.resolvedUrl("bin/")).replace(/^file:\/\//, ""))
  readonly property string python: "/usr/bin/python3"
  readonly property string terminalLauncher: "/usr/bin/omarchy-launch-floating-terminal-with-presentation"
  readonly property int rowsPerSection: Number(setting("rowsPerSection", 6))
  readonly property int refreshSec: Number(setting("refreshSec", 3))

  property var sections: []
  property var notes: ({})
  property var expanded: ({})
  readonly property var display: Model.flatten(sections, rowsPerSection, expanded)
  readonly property int liveCount: Model.liveCount(sections)
  readonly property int needsCount: Model.needsCount(sections)
  readonly property string summaryText: Model.summary(sections)

  // ---------------------------------------------------------------- data

  Process {
    id: runner
    command: [root.python, root.pluginBin + "cockpit"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = Model.parse(text)
        if (!parsed.ok) {
          root.notes = { "cockpit": "the runner produced no usable output" }
          return
        }
        root.sections = parsed.sections
        root.notes = parsed.notes
      }
    }
    onStarted: runnerWatchdog.restart()
    onExited: runnerWatchdog.stop()
  }

  function refresh() { if (!runner.running) runner.running = true }

  // Providers run in parallel, each under a 30 s ceiling, so a runner still going after
  // 45 s is stuck -- and since a refresh is skipped while one is running, a stuck runner
  // would otherwise freeze the hub for good. TERM first, KILL if that is ignored.
  Timer {
    id: runnerWatchdog
    interval: 45000
    onTriggered: root.stopProcess(runner, runnerKill)
  }
  Timer {
    id: runnerKill
    interval: 2000
    onTriggered: if (runner.running) runner.signal(9)
  }

  function stopProcess(proc, killTimer) {
    if (!proc.running) return
    proc.signal(15)
    killTimer.restart()
  }

  // The daemon is what makes "which agent is busy" accurate -- hyprctl serves a cached
  // title. Started from here rather than an autostart entry so it dies with the shell.
  Process {
    id: agentd
    command: [root.python, root.pluginBin + "cockpit-agentd"]
    running: true
    // It exits when Hyprland's socket closes or another copy already holds it; try again
    // later rather than losing busy-detection until the shell restarts.
    onExited: agentdRestart.restart()
  }

  Timer {
    id: agentdRestart
    interval: 30000
    onTriggered: if (!agentd.running) agentd.running = true
  }

  Timer {
    // Only while open: a hub nobody is looking at should not be running providers. The
    // bar count refreshes on the slow timer below instead.
    running: root.opened
    interval: Math.max(1, root.refreshSec) * 1000
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    // Keeps the bar dot honest while closed, cheaply -- providers cache themselves, so
    // this mostly reads their cache rather than re-running anything. Five seconds because
    // the dot turns urgent when an agent is waiting on you, and that is worth seeing soon.
    running: !root.opened
    interval: 5000
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // A row's action is a structured object, never a shell string. It travels to the
  // helper over stdin, which validates it against an allow-list and performs it; the one
  // thing it hands back is a presentation-terminal argv, started detached here because a
  // terminal must outlive the helper.
  Process {
    id: actionProc
    property string payload: ""
    command: [root.python, root.pluginBin + "cockpit", "action"]
    stdinEnabled: true
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var argv = Model.terminalLaunch(text, root.terminalLauncher)
        if (argv) Quickshell.execDetached(argv)
      }
    }
    onStarted: {
      write(payload + "\n")
      payload = ""
      actionWatchdog.restart()
    }
    onExited: actionWatchdog.stop()
  }

  Timer {
    id: actionWatchdog
    interval: 10000
    onTriggered: root.stopProcess(actionProc, actionKill)
  }
  Timer {
    id: actionKill
    interval: 2000
    onTriggered: if (actionProc.running) actionProc.signal(9)
  }

  function runAction(actionJson) {
    if (!actionJson || actionProc.running) return
    actionProc.payload = actionJson
    actionProc.running = true
    root.close()
  }

  function toggleSection(name) {
    var next = {}
    for (var k in expanded) next[k] = expanded[k]
    next[name] = !next[name]
    expanded = next
  }

  // ---------------------------------------------------------------- ui

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    popoutSwitching: root.popoutSwitching
    popoutSwitchClosing: root.popoutSwitchClosing
    contentWidth: panel.fittedContentWidth(Style.space(520))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    // Nothing here is typed into, so the house key catcher is the right primitive: its
    // vim bindings are a feature for a list you only steer.
    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function (direction) { root.switchPanel(direction) }

      ColumnLayout {
        id: column
        width: parent.width
        spacing: Style.space(4)

        Repeater {
          model: root.display

          delegate: Item {
            id: line
            required property var modelData
            Layout.fillWidth: true
            implicitHeight: modelData.kind === "header" ? Style.space(26)
                          : (modelData.kind === "more" ? Style.space(20) : Style.space(34))

            // ---- section heading
            Row {
              visible: line.modelData.kind === "header"
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(7)
              Text {
                text: line.modelData.glyph || ""
                color: Color.accent
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                text: line.modelData.section || ""
                color: root.barForeground
                opacity: 0.9
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }
              Text {
                text: line.modelData.count !== undefined ? line.modelData.count : ""
                color: root.barForeground
                opacity: 0.4
                font.pixelSize: Style.font.caption
              }
            }

            // ---- "+N more"
            Text {
              visible: line.modelData.kind === "more"
              anchors.verticalCenter: parent.verticalCenter
              x: Style.space(24)
              text: line.modelData.hidden !== undefined ? "+" + line.modelData.hidden + " more" : ""
              color: root.barForeground
              opacity: 0.4
              font.pixelSize: Style.font.caption
            }

            // ---- a row
            Rectangle {
              visible: line.modelData.kind === "row"
              anchors.fill: parent
              radius: Style.cornerRadius
              color: rowArea.containsMouse ? Style.hoverFillFor(root.barForeground, Color.accent) : "transparent"

              RowLayout {
                anchors.fill: parent
                anchors.leftMargin: Style.space(10)
                anchors.rightMargin: Style.space(10)
                spacing: Style.space(8)

                Text {
                  text: line.modelData.glyph || ""
                  // "needs" (an agent waiting on you) and "warn" (true all day) share the
                  // urgent tint; only "needs" also colours its detail line and the bar dot.
                  color: line.modelData.state === "busy" ? Color.accent
                       : ((line.modelData.state === "warn" || line.modelData.state === "needs") ? Color.urgent : root.barForeground)
                  opacity: line.modelData.state === "idle" ? 0.55 : 1.0
                  font.pixelSize: Style.font.body
                  Layout.preferredWidth: Style.space(16)
                }

                ColumnLayout {
                  Layout.fillWidth: true
                  spacing: 0

                  Text {
                    Layout.fillWidth: true
                    text: line.modelData.title || ""
                    color: root.barForeground
                    font.pixelSize: Style.font.bodySmall
                    elide: Text.ElideRight
                  }

                  RowLayout {
                    Layout.fillWidth: true
                    spacing: Style.space(6)

                    // Only drawn when a provider actually reported progress, so a row
                    // without it is not padded with a bar pretending to mean something.
                    Rectangle {
                      visible: (line.modelData.progress !== undefined) && line.modelData.progress >= 0
                      Layout.preferredWidth: Style.space(60)
                      Layout.preferredHeight: Style.space(3)
                      radius: height / 2
                      color: Qt.rgba(root.barForeground.r, root.barForeground.g, root.barForeground.b, 0.2)
                      Rectangle {
                        width: parent.width * ((line.modelData.progress || 0) / 100)
                        height: parent.height
                        radius: height / 2
                        color: Color.accent
                        Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                      }
                    }

                    Text {
                      Layout.fillWidth: true
                      text: line.modelData.detail || ""
                      color: line.modelData.state === "needs" ? Color.urgent : root.barForeground
                      opacity: line.modelData.state === "needs" ? 0.9 : 0.5
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideRight
                    }
                  }
                }
              }
            }

            MouseArea {
              id: rowArea
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: (line.modelData.kind !== "row" || line.modelData.action)
                ? Qt.PointingHandCursor : Qt.ArrowCursor
              onClicked: {
                if (line.modelData.kind === "row") root.runAction(line.modelData.action || "")
                else root.toggleSection(line.modelData.section)
              }
            }
          }
        }

        Text {
          Layout.fillWidth: true
          visible: root.display.length === 0
          horizontalAlignment: Text.AlignHCenter
          topPadding: Style.space(18)
          bottomPadding: Style.space(18)
          text: "Nothing in flight"
          color: root.barForeground
          opacity: 0.5
          font.pixelSize: Style.font.body
        }

        // A provider that broke is worth one quiet line: silently missing a section is
        // worse than an ugly one, because you would trust a hub that is lying by omission.
        Repeater {
          model: Model.brokenNotes(root.notes)
          delegate: Text {
            Layout.fillWidth: true
            text: "! " + modelData
            color: Color.urgent
            opacity: 0.75
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
      }
    }
  }
}
