import QtQuick
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as P5Support
import "../code/CockpitModel.js" as Model

// Cockpit for Plasma: one place for what the computer is doing right now.
//
// The same hub as the Omarchy edition (bin/cockpit and its providers) runs on a timer and prints one document; the
// panel draws it. A row's action is a structured object handed to `cockpit action` on stdin, which validates it
// and answers with the program to start (a terminal, a browser, a file manager, a resumed chat); that answer is
// started only if it has one of the exact shapes CockpitModel allows.
PlasmoidItem {
  id: root

  readonly property string code: String(Qt.resolvedUrl("../code")).replace("file://", "")
  readonly property int rowsPerSection: Math.max(1, Math.min(50, Number(Plasmoid.configuration.rowsPerSection) || 6))
  readonly property int refreshSec: Math.max(1, Math.min(60, Number(Plasmoid.configuration.refreshSec) || 3))
  readonly property bool agentAlerts: Plasmoid.configuration.agentAlerts !== false

  // What the hub is given of Plasma's environment: where home, config, data and the runtime directory are.
  // COCKPIT_PLAIN_LAUNCH: this is Plasma, so actions open without uwsm even where uwsm is installed.
  readonly property string envPrefix: "/usr/bin/env -i PATH=/usr/bin:/bin PYTHONIOENCODING=utf-8 COCKPIT_PLAIN_LAUNCH=1 HOME=\"$HOME\" LANG=\"$LANG\""
    + " XDG_RUNTIME_DIR=\"$XDG_RUNTIME_DIR\" XDG_CONFIG_HOME=\"$XDG_CONFIG_HOME\" XDG_DATA_HOME=\"$XDG_DATA_HOME\" "

  function quote(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'" }
  function helper(name) { return root.envPrefix + "/usr/bin/python3 -I " + root.quote(root.code + "/bin/" + name) }

  P5Support.DataSource {
    id: runner
    engine: "executable"
    property var onDone: ({})
    onNewData: function (source, data) {
      disconnectSource(source)
      var cb = runner.onDone[source]
      delete runner.onDone[source]
      if (cb) cb(Number(data["exit code"]), String(data["stdout"] || ""))
    }
  }

  function run(command, done) {
    if (runner.onDone[command] !== undefined) return false
    runner.onDone[command] = done || null
    runner.connectSource(command)
    return true
  }

  // ---------------------------------------------------------------- the hub

  property var sections: []
  property var notes: ({})
  property string lastOutput: ""
  property bool refreshing: false

  // A refresh asked for while one runs (after a click, say) runs right after it, so its result is not lost.
  property bool refreshAgain: false

  function refresh() {
    if (root.refreshing) { root.refreshAgain = true; return }
    root.refreshing = true
    root.run("/usr/bin/timeout -k 2 25 " + root.helper("cockpit"), function (code, out) {
      root.refreshing = false
      if (root.refreshAgain) { root.refreshAgain = false; Qt.callLater(root.refresh) }
      // The same document again leaves the rows alone, so the one under the pointer keeps its buttons.
      if (out === root.lastOutput) return
      var parsed = Model.parse(out)
      if (!parsed.ok) {
        root.notes = { "cockpit": "the hub produced no usable output" }
        root.lastOutput = ""        // so the same good answer next time clears this note
        return
      }
      root.lastOutput = out
      root.sections = parsed.sections
      root.notes = parsed.notes
    })
  }

  // While open, at the chosen pace; closed, every five seconds, which mostly reads the providers' caches and keeps
  // the icon honest when a session starts waiting for you.
  Timer {
    interval: (root.expanded ? root.refreshSec : 5) * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  onExpandedChanged: function () { if (root.expanded) root.refresh() }

  function refreshSoon() { soon.restart() }
  Timer { id: soon; interval: 400; onTriggered: root.refresh() }

  // ---------------------------------------------------------------- actions

  function act(actionJson, kind) {
    if (!actionJson) return
    root.run("/usr/bin/printf '%s\\n' " + root.quote(actionJson) + " | /usr/bin/timeout -k 2 20 " + root.helper("cockpit") + " action"
             + " # " + Date.now(), function (code, out) {
      var argv = Model.launchArgv(out.trim(), "", true)
      // In a transient unit of its own: a terminal or browser started here must outlive a plasmashell restart, which
      // would otherwise take everything left in its cgroup with it.
      if (argv) root.run("/usr/bin/systemd-run --user --collect --quiet --slice=app.slice -- " + argv.map(root.quote).join(" ")
                         + " >/dev/null 2>&1 # " + Date.now(), null)
      if (Model.keepsOpen(kind)) root.refreshSoon()
      else root.expanded = false
    })
  }

  // ---------------------------------------------------------------- the agents daemon

  // It keeps the chats you can resume and says when a session needs you. Without Hyprland it reads only the
  // sessions, so what each is doing is not shown; one runs per user, so a second widget's exits at once.
  readonly property string agentdCommand: root.helper("cockpit-agentd") + (root.agentAlerts ? "" : " --no-alerts")
  property string runningAgentd: ""
  P5Support.DataSource {
    id: agentd
    engine: "executable"
    onNewData: function (source, data) {
      if (source !== root.runningAgentd) return
      disconnectSource(source)
      root.runningAgentd = ""
      agentdAgain.restart()
    }
  }
  function startAgentd() {
    if (root.runningAgentd !== "") agentd.disconnectSource(root.runningAgentd)
    root.runningAgentd = root.agentdCommand
    agentd.connectSource(root.runningAgentd)
  }
  Timer { id: agentdAgain; interval: 30000; onTriggered: root.startAgentd() }
  onAgentdCommandChanged: Qt.callLater(root.startAgentd)
  Component.onCompleted: root.startAgentd()

  // ---------------------------------------------------------------- the icon

  readonly property int live: Model.liveCount(root.sections)
  readonly property int needs: Model.needsCount(root.sections)

  Plasmoid.icon: "dashboard-show"
  Plasmoid.status: root.needs > 0 ? PlasmaCore.Types.NeedsAttentionStatus
                 : root.live > 0 ? PlasmaCore.Types.ActiveStatus : PlasmaCore.Types.PassiveStatus
  toolTipMainText: "Cockpit"
  toolTipSubText: Model.summary(root.sections)

  compactRepresentation: CompactView { cockpit: root }
  fullRepresentation: FullView { cockpit: root }
}
