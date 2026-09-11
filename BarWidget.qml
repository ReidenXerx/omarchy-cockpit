import QtQuick
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "reidenxerx.cockpit"

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function togglePanel() { if (panelLoader.item && panelLoader.item.toggle) panelLoader.item.toggle() }
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  function open() { if (panelLoader.item && panelLoader.item.open) panelLoader.item.open() }
  function close() { if (panelLoader.item && panelLoader.item.close) panelLoader.item.close() }
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  readonly property int liveCount: panelLoader.item ? panelLoader.item.liveCount : 0
  readonly property int needsCount: panelLoader.item ? panelLoader.item.needsCount : 0

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: { root.injectPanel(); Qt.callLater(root.injectPanel) }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // The count is only things actually in motion. Dirty repos and pending updates are
    // true all day, and a badge that is always lit is a badge you stop reading.
    // md-view_dashboard (U+F056E), resolved from the font's own cmap rather than guessed:
    // md-speedometer (U+F04C5) is what power-profile widgets use, and two icons meaning
    // different things should not look the same.
    //
    // The glyph alone, never glyph + count. BarIconButton optically centres its text in a
    // fixed slot, so appending " 3" makes it centre glyph-and-digit as one unit: the glyph
    // drifts left and the digit crowds the neighbouring icon. The count goes in the dot
    // below, which is drawn over the button and stays out of the text flow.
    text: "󰕮"
    // No slotSize override: BarIconButton defaults to Style.bar.iconSlot (27), which is
    // what the rest of the bar uses. statusSlot is 21, and setting it made this widget
    // six logical pixels narrower than its neighbours -- visible as a tighter gap.
    foreground: root.bar ? root.bar.barForeground : Color.foreground
    tooltipText: panelLoader.item ? panelLoader.item.summaryText : "Cockpit"
    onPressed: function (b) { root.togglePanel() }

    // A dot rather than a number, the way the bell marks unread. It sits on top of the
    // button instead of inside its text, so the slot's optical centring is untouched and
    // the spacing stays even with every other icon in the bar.
    // Urgent while an agent is waiting on you -- a question or a permission prompt -- since
    // that is the one thing here that stalls until you act.
    Rectangle {
      visible: root.liveCount > 0 || root.needsCount > 0
      width: Style.space(6)
      height: width
      radius: width / 2
      color: root.needsCount > 0 ? Color.urgent : Color.accent
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.rightMargin: Style.space(3)
      anchors.topMargin: Style.space(5)
    }
  }
}
