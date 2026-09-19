import QtQuick
import QtQuick.Controls
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
  // A notification when an agent starts waiting for you or finishes a long turn.
  readonly property bool agentAlerts: String(setting("agentAlerts", true)) !== "false"

  property var sections: []
  property var notes: ({})
  property var expanded: ({})
  property string lastOutput: ""
  property bool refreshAgain: false
  // The button waiting for its second click, by its action. Rows are rebuilt whenever the
  // runner reports a change, so this cannot live in a row.
  property string confirmAction: ""
  readonly property var display: Model.flatten(sections, rowsPerSection, expanded)
  readonly property int liveCount: Model.liveCount(sections)
  readonly property int needsCount: Model.needsCount(sections)
  readonly property string summaryText: Model.summary(sections)

  onOpenedChanged: if (!opened) confirmAction = ""

  // ---------------------------------------------------------------- data

  // A child process starts with whatever the shell was started with, and the shell is
  // long-lived, so LD_PRELOAD, PYTHONPATH and PYTHONHOME would all reach an interpreter
  // this plugin then trusts. Each helper is handed an explicit environment instead, and
  // python runs isolated on top of it (-I); the helpers put their own directory on
  // sys.path themselves, so nothing depends on -P's default.
  readonly property var childEnv: {
    const env = { "PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8" }
    for (const name of ["HOME", "LANG", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
                        "XDG_DATA_HOME", "HYPRLAND_INSTANCE_SIGNATURE"]) {
      const value = Quickshell.env(name)
      if (value) env[name] = value
    }
    return env
  }

  Process {
    id: runner
    clearEnvironment: true
    environment: root.childEnv
    command: [root.python, "-I", root.pluginBin + "cockpit"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        // The same document again leaves the rows alone, so the one under the pointer keeps
        // its buttons.
        if (text === root.lastOutput) return
        var parsed = Model.parse(text)
        if (!parsed.ok) {
          root.notes = { "cockpit": "the runner produced no usable output" }
          return
        }
        root.lastOutput = text
        root.sections = parsed.sections
        root.notes = parsed.notes
      }
    }
    onStarted: runnerWatchdog.restart()
    onExited: {
      runnerWatchdog.stop()
      if (root.refreshAgain) {
        root.refreshAgain = false
        Qt.callLater(root.refresh)
      }
    }
  }

  function refresh() { if (!runner.running) runner.running = true }

  // After a click that changes what the hub shows: now, or right after the run in progress.
  function refreshSoon() {
    if (runner.running) root.refreshAgain = true
    else runner.running = true
  }

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
  // title -- and what sends the agent notifications. Started from here rather than an
  // autostart entry so it dies with the shell.
  Process {
    id: agentd
    clearEnvironment: true
    environment: root.childEnv
    command: root.agentAlerts ? [root.python, "-I", root.pluginBin + "cockpit-agentd"]
                              : [root.python, "-I", root.pluginBin + "cockpit-agentd", "--no-alerts"]
    running: true
    // It exits when Hyprland's socket closes or another copy already holds it; try again
    // later rather than losing busy-detection until the shell restarts.
    onExited: agentdRestart.restart()
  }

  Timer {
    id: agentdRestart
    interval: 30000
    onTriggered: {
      interval = 30000
      if (!agentd.running) agentd.running = true
    }
  }

  // Turning the notifications on or off restarts the daemon with the new flag, in a second.
  onAgentAlertsChanged: {
    agentdRestart.interval = 1000
    if (agentd.running) agentd.signal(15)
    else agentdRestart.restart()
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
  // helper over stdin, which validates it against an allow-list and performs it; what it
  // hands back is an argv to start detached here -- a terminal, a browser, a file manager,
  // a resumed chat -- because those must outlive the helper.
  Process {
    id: actionProc
    clearEnvironment: true
    environment: root.childEnv
    property string payload: ""
    property string kind: ""
    command: [root.python, "-I", root.pluginBin + "cockpit", "action"]
    stdinEnabled: true
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var argv = Model.launchArgv(text, root.terminalLauncher)
        if (argv) Quickshell.execDetached(argv)
      }
    }
    onStarted: {
      write(payload + "\n")
      payload = ""
      actionWatchdog.restart()
    }
    onExited: {
      actionWatchdog.stop()
      if (Model.keepsOpen(kind)) root.refreshSoon()
    }
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

  function runAction(actionJson, kind) {
    if (!actionJson || actionProc.running) return
    root.confirmAction = ""
    actionProc.payload = actionJson
    actionProc.kind = kind || ""
    actionProc.running = true
    if (!Model.keepsOpen(kind)) root.close()
  }

  // A button that asks first turns into its question on the first click and acts on the
  // second, within a few seconds.
  function pressButton(button) {
    if (button.confirm && root.confirmAction !== button.action) {
      root.confirmAction = button.action
      confirmExpiry.restart()
      return
    }
    root.runAction(button.action, button.kind)
  }

  Timer {
    id: confirmExpiry
    interval: 4000
    onTriggered: root.confirmAction = ""
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
    contentWidth: panel.fittedContentWidth(Style.space(560))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    // Nothing here is typed into, so the house key catcher is the right primitive: its
    // vim bindings are a feature for a list you only steer.
    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function (direction) { root.switchPanel(direction) }

      // Ten sections can outgrow the screen: the card stops at its edge and the list scrolls
      // inside, leaving room for the scroll bar beside the row buttons.
      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        ColumnLayout {
          id: column
          width: scroller.width - (scroller.interactive ? Style.space(10) : 0)
          spacing: Style.space(4)

          Repeater {
            model: root.display

            delegate: Item {
              id: line
              required property var modelData
              // What the hovered button does, shown in place of the detail line.
              property string buttonHint: ""
              // The button of this row that is waiting for its second click, if any.
              readonly property var asking: {
                var list = line.modelData.buttons || []
                for (var i = 0; i < list.length; i++) {
                  if (root.confirmAction !== "" && list[i].action === root.confirmAction) return list[i]
                }
                return null
              }
              Layout.fillWidth: true
              implicitHeight: modelData.kind === "header" ? Style.space(26)
                            : (modelData.kind === "more" ? Style.space(20) : Style.space(34))

              HoverHandler { id: lineHover }

              // Under everything else, so a row's buttons and a heading's dismissed count take
              // their own clicks and the rest of the line falls through to this.
              MouseArea {
                id: rowArea
                anchors.fill: parent
                cursorShape: (line.modelData.kind !== "row" || line.modelData.action)
                  ? Qt.PointingHandCursor : Qt.ArrowCursor
                onClicked: {
                  if (line.modelData.kind === "row") root.runAction(line.modelData.action || "", line.modelData.actionKind)
                  else root.toggleSection(line.modelData.section)
                }
              }

              // ---- section heading
              Row {
                visible: line.modelData.kind === "header"
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(7)
                Text {
                  // Window titles, SSIDs and process names are strings the system
                  // handed us. Rendered literally, never interpreted as markup.
                  textFormat: Text.PlainText
                  text: line.modelData.glyph || ""
                  color: Color.accent
                  font.pixelSize: Style.font.bodySmall
                }
                Text {
                  // Window titles, SSIDs and process names are strings the system
                  // handed us. Rendered literally, never interpreted as markup.
                  textFormat: Text.PlainText
                  text: line.modelData.section || ""
                  color: root.barForeground
                  opacity: 0.9
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
                Text {
                  // Window titles, SSIDs and process names are strings the system
                  // handed us. Rendered literally, never interpreted as markup.
                  textFormat: Text.PlainText
                  text: line.modelData.count ? line.modelData.count : ""
                  color: root.barForeground
                  opacity: 0.4
                  font.pixelSize: Style.font.caption
                }
                // Rows you dismissed stay counted here, and a click brings them all back.
                Text {
                  // Window titles, SSIDs and process names are strings the system
                  // handed us. Rendered literally, never interpreted as markup.
                  textFormat: Text.PlainText
                  visible: (line.modelData.dismissed || 0) > 0
                  text: dismissedArea.containsMouse ? "bring back " + line.modelData.dismissed
                                                    : line.modelData.dismissed + " dismissed"
                  color: dismissedArea.containsMouse ? Color.accent : root.barForeground
                  opacity: dismissedArea.containsMouse ? 1.0 : 0.4
                  font.pixelSize: Style.font.caption

                  MouseArea {
                    id: dismissedArea
                    anchors.fill: parent
                    anchors.margins: -Style.space(4)
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.runAction(line.modelData.undismiss || "", "undismiss")
                  }
                }
              }

              // ---- "+N more"
              Text {
                // Window titles, SSIDs and process names are strings the system
                // handed us. Rendered literally, never interpreted as markup.
                textFormat: Text.PlainText
                visible: line.modelData.kind === "more"
                anchors.verticalCenter: parent.verticalCenter
                x: Style.space(26)
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
                color: lineHover.hovered ? Style.hoverFillFor(root.barForeground, Color.accent) : "transparent"

                RowLayout {
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(10)
                  anchors.rightMargin: Style.space(6)
                  spacing: Style.space(8)

                  Text {
                    // Window titles, SSIDs and process names are strings the system
                    // handed us. Rendered literally, never interpreted as markup.
                    textFormat: Text.PlainText
                    text: line.modelData.glyph || ""
                    // "needs" (an agent waiting on you) and "warn" (true all day) share the
                    // urgent tint; only "needs" also colours its detail line and the bar dot.
                    color: line.modelData.state === "busy" ? Color.accent
                         : ((line.modelData.state === "warn" || line.modelData.state === "needs") ? Color.urgent : root.barForeground)
                    opacity: line.modelData.state === "idle" ? 0.55 : 1.0
                    font.pixelSize: Style.font.body
                    horizontalAlignment: Text.AlignHCenter
                    Layout.preferredWidth: Style.space(18)
                  }

                  ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    Text {
                      // Window titles, SSIDs and process names are strings the system
                      // handed us. Rendered literally, never interpreted as markup.
                      textFormat: Text.PlainText
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

                      // The detail, or what the hovered button does, or the question a pressed
                      // one is asking.
                      Text {
                        // Window titles, SSIDs and process names are strings the system
                        // handed us. Rendered literally, never interpreted as markup.
                        textFormat: Text.PlainText
                        Layout.fillWidth: true
                        text: line.asking ? line.asking.confirm : (line.buttonHint || line.modelData.detail || "")
                        color: (line.asking || line.modelData.state === "needs") ? Color.urgent : root.barForeground
                        opacity: (line.asking || line.modelData.state === "needs") ? 0.9 : (line.buttonHint ? 0.8 : 0.5)
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                      }
                    }
                  }

                  // A row's buttons show while the pointer is on it, or while one is asking.
                  Row {
                    visible: (line.modelData.buttons || []).length > 0 && (lineHover.hovered || line.asking !== null)
                    spacing: Style.space(2)
                    Layout.alignment: Qt.AlignVCenter

                    Repeater {
                      model: line.modelData.buttons || []

                      delegate: Rectangle {
                        id: chip
                        required property var modelData
                        readonly property bool armed: root.confirmAction !== "" && root.confirmAction === chip.modelData.action
                        width: Style.space(26)
                        height: Style.space(26)
                        radius: Style.cornerRadius
                        color: (chipArea.containsMouse || chip.armed)
                          ? Style.hoverFillFor(root.barForeground, chip.armed ? Color.urgent : Color.accent) : "transparent"

                        Text {
                          // Window titles, SSIDs and process names are strings the system
                          // handed us. Rendered literally, never interpreted as markup.
                          textFormat: Text.PlainText
                          anchors.centerIn: parent
                          text: chip.modelData.glyph || "?"
                          color: chip.armed ? Color.urgent : root.barForeground
                          opacity: (chipArea.containsMouse || chip.armed) ? 1.0 : 0.65
                          font.pixelSize: Style.font.body
                        }

                        MouseArea {
                          id: chipArea
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: Qt.PointingHandCursor
                          onContainsMouseChanged: {
                            if (containsMouse) line.buttonHint = chip.modelData.label || ""
                            else if (line.buttonHint === (chip.modelData.label || "")) line.buttonHint = ""
                          }
                          onClicked: root.pressButton(chip.modelData)
                        }
                      }
                    }
                  }
                }
              }
            }
          }

          Text {
            // Window titles, SSIDs and process names are strings the system
            // handed us. Rendered literally, never interpreted as markup.
            textFormat: Text.PlainText
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
}
