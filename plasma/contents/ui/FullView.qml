import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.extras as PlasmaExtras
import org.kde.kirigami as Kirigami
import "../code/CockpitModel.js" as Model

// The panel: every section the providers report, a few rows each, with what can be clicked. Everything shown is a
// provider's string, so all of it is plain text.
PlasmaExtras.Representation {
  id: view

  required property var cockpit

  Layout.preferredWidth: Kirigami.Units.gridUnit * 28
  Layout.preferredHeight: Kirigami.Units.gridUnit * 34
  Layout.minimumWidth: Kirigami.Units.gridUnit * 20
  Layout.minimumHeight: Kirigami.Units.gridUnit * 16

  readonly property var c: view.cockpit
  property var expanded: ({})
  readonly property var items: Model.flatten(view.c.sections, view.c.rowsPerSection, view.expanded)
  readonly property var broken: Model.brokenNotes(view.c.notes)
  readonly property color faint: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.65)
  readonly property real smallPt: Math.max(7, Math.round(sizer.font.pointSize * 0.9))
  PlasmaComponents.Label { id: sizer; visible: false }

  // A button that asks first: the first click shows its question, a second within a few seconds acts.
  property string confirming: ""
  Timer { id: confirmTimeout; interval: 5000; onTriggered: view.confirming = "" }

  readonly property var icons: ({ "Agents": "code-context", "Reboot": "system-reboot", "Transfers": "folder-download",
    "Jobs": "system-run", "Git": "vcs-commit", "Ports": "network-server", "Updates": "system-software-update",
    "GPU": "video-card-inactive", "Tailnet": "network-vpn", "Network": "network-wireless",
    "Devices": "drive-removable-media", "Disks": "drive-harddisk" })

  readonly property var kindIcons: ({ "open-folder": "folder-open", "open-url": "internet-web-browser",
    "stop-process": "process-stop", "resume-agent": "utilities-terminal", "terminal": "utilities-terminal",
    "run": "system-run", "dismiss": "view-hidden", "undismiss": "view-visible", "focus-window": "window" })

  function stateColor(state) {
    switch (state) {
    case "needs": return Kirigami.Theme.negativeTextColor
    case "warn": return Kirigami.Theme.neutralTextColor
    case "busy": return Kirigami.Theme.highlightColor
    case "ok": return Kirigami.Theme.positiveTextColor
    default: return Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g, Kirigami.Theme.textColor.b, 0.3)
    }
  }

  function toggleSection(name) {
    var next = Object.assign({}, view.expanded)
    next[name] = !next[name]
    view.expanded = next
  }

  header: PlasmaExtras.PlasmoidHeading {
    RowLayout {
      anchors.fill: parent
      Kirigami.Heading {
        Layout.fillWidth: true
        level: 3
        textFormat: Text.PlainText
        elide: Text.ElideRight
        text: Model.summary(view.c.sections)
      }
      PlasmaComponents.BusyIndicator {
        implicitWidth: Kirigami.Units.iconSizes.small
        implicitHeight: implicitWidth
        running: view.c.refreshing && view.c.sections.length === 0
        visible: running
      }
    }
  }

  PlasmaComponents.ScrollView {
    id: scroller
    anchors.fill: parent
    QQC2.ScrollBar.horizontal.policy: QQC2.ScrollBar.AlwaysOff
    contentWidth: availableWidth

    ColumnLayout {
      width: scroller.availableWidth
      spacing: Kirigami.Units.smallSpacing

      PlasmaComponents.Label {
        Layout.fillWidth: true
        visible: view.c.sections.length === 0 && !view.c.refreshing
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: view.faint
        text: "Nothing in flight."
      }

      Repeater {
        model: view.items
        delegate: Loader {
          required property var modelData
          Layout.fillWidth: true
          sourceComponent: modelData.kind === "header" ? headerItem : modelData.kind === "more" ? moreItem : rowItem
          property var item_: modelData
        }
      }

      PlasmaComponents.Label {
        Layout.fillWidth: true
        Layout.topMargin: Kirigami.Units.largeSpacing
        visible: view.broken.length > 0
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: Kirigami.Theme.neutralTextColor
        font.pointSize: view.smallPt
        text: "Not shown: " + view.broken.join(" · ")
      }
    }
  }

  Component {
    id: headerItem
    RowLayout {
      readonly property var it: parent.item_
      spacing: Kirigami.Units.smallSpacing
      Kirigami.Icon {
        Layout.topMargin: Kirigami.Units.largeSpacing
        implicitWidth: Kirigami.Units.iconSizes.small
        implicitHeight: implicitWidth
        source: view.icons[it.section] || "dashboard-show"
      }
      PlasmaComponents.Label {
        Layout.fillWidth: true
        Layout.topMargin: Kirigami.Units.largeSpacing
        textFormat: Text.PlainText
        text: it.section
        font.bold: true
      }
      PlasmaComponents.ToolButton {
        Layout.topMargin: Kirigami.Units.largeSpacing
        visible: it.dismissed > 0 && it.undismiss !== ""
        text: it.dismissed + " hidden · show"
        onClicked: view.c.act(it.undismiss, "undismiss")
      }
    }
  }

  Component {
    id: moreItem
    PlasmaComponents.ToolButton {
      readonly property var it: parent.item_
      text: it.hidden + " more"
      onClicked: view.toggleSection(it.section)
    }
  }

  Component {
    id: rowItem
    PlasmaComponents.ItemDelegate {
      id: row
      readonly property var it: parent.item_
      hoverEnabled: true
      enabled: true
      onClicked: if (row.it.action) view.c.act(row.it.action, row.it.actionKind)
      contentItem: ColumnLayout {
        spacing: 2
        RowLayout {
          Layout.fillWidth: true
          spacing: Kirigami.Units.smallSpacing * 2
          Rectangle {
            implicitWidth: Kirigami.Units.smallSpacing * 2
            implicitHeight: implicitWidth
            radius: implicitWidth / 2
            color: view.stateColor(row.it.state)
          }
          ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            PlasmaComponents.Label {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              elide: Text.ElideRight
              text: row.it.title
            }
            PlasmaComponents.Label {
              Layout.fillWidth: true
              visible: text !== ""
              textFormat: Text.PlainText
              elide: Text.ElideRight
              color: view.faint
              font.pointSize: view.smallPt
              text: row.it.detail
            }
          }
          Repeater {
            model: row.it.buttons
            delegate: PlasmaComponents.ToolButton {
              required property var modelData
              required property int index
              // The action itself (it names the pid), so two rows with the same title never share a question.
              readonly property string key: modelData.action
              readonly property bool asking: view.confirming === key
              // The provider's glyph is a Nerd Font character Plasma may not have: an icon for what it does.
              icon.name: asking ? "dialog-ok-apply" : (view.kindIcons[modelData.kind] || "go-next")
              display: asking ? QQC2.AbstractButton.TextBesideIcon : QQC2.AbstractButton.IconOnly
              text: asking ? "Sure?" : modelData.label
              Accessible.name: modelData.label
              PlasmaComponents.ToolTip.text: asking ? modelData.confirm : modelData.label
              PlasmaComponents.ToolTip.visible: hovered || view.confirming === key
              onClicked: {
                if (modelData.confirm && view.confirming !== key) {
                  view.confirming = key
                  confirmTimeout.restart()
                  return
                }
                view.confirming = ""
                view.c.act(modelData.action, modelData.kind)
              }
            }
          }
        }
        PlasmaComponents.ProgressBar {
          Layout.fillWidth: true
          Layout.leftMargin: Kirigami.Units.smallSpacing * 4
          visible: row.it.progress >= 0
          from: 0
          to: 100
          value: Math.max(0, row.it.progress)
        }
      }
    }
  }
}
