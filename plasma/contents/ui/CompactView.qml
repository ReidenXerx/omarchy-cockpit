import QtQuick
import org.kde.kirigami as Kirigami

// The icon, with a dot when something is in motion: accent for work going on, the warning colour when a session
// is waiting for you.
MouseArea {
  id: compact

  required property var cockpit

  hoverEnabled: true
  onClicked: compact.cockpit.expanded = !compact.cockpit.expanded

  Kirigami.Icon {
    anchors.fill: parent
    source: "dashboard-show"
    active: compact.containsMouse
  }

  Rectangle {
    visible: compact.cockpit.live > 0 || compact.cockpit.needs > 0
    anchors.right: parent.right
    anchors.top: parent.top
    width: Math.max(6, Math.round(parent.width * 0.32))
    height: width
    radius: width / 2
    color: compact.cockpit.needs > 0 ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.highlightColor
    border.width: 1
    border.color: Kirigami.Theme.backgroundColor
  }
}
