import QtQuick
import QtQuick.Controls

Button {
    id: control
    QStyle { id: style }
    property string iconName: ""
    property string tone: "secondary"
    property bool selected: false
    property int labelSize: 15
    property bool centered: true
    readonly property color ink: !enabled ? style.faint : selected || tone === "primary" ? style.accent : style.text
    implicitHeight: 44
    implicitWidth: Math.max(44, Math.ceil(layoutLabel.advanceWidth) + (iconName ? 32 : 0) + 32)
    // Keep the established hit area while reducing only the displayed type.
    TextMetrics { id: layoutLabel; text: control.text; font.family: style.family; font.pixelSize: control.labelSize === 15 ? 16 : control.labelSize; font.weight: control.selected ? Font.Medium : Font.Normal }
    padding: 0
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    Accessible.name: text
    background: Rectangle {
        radius: 9
        color: control.selected ? style.accentFill : control.down ? "#E2E5E9" : control.hovered ? style.hover :
               control.tone === "quiet" || control.tone === "nav" ? "transparent" : style.background
        border.width: control.activeFocus ? 2 : control.tone === "quiet" || control.tone === "nav" ? 0 : 1
        border.color: control.activeFocus ? style.accent : style.line
        Behavior on color { ColorAnimation { duration: 100 } }
    }
    contentItem: Item {
        Row {
            anchors.verticalCenter: parent.verticalCenter
            x: control.centered ? (parent.width - width) / 2 : 20
            spacing: control.tone === "nav" ? 18 : 12
            QSymbol { visible: control.iconName !== ""; name: control.iconName || "monitor"; tint: control.ink; size: control.tone === "nav" ? 28 : 22; anchors.verticalCenter: parent.verticalCenter }
            QText { id: label; text: control.text; color: control.ink; font.pixelSize: control.labelSize; font.weight: control.selected ? Font.Medium : Font.Normal; anchors.verticalCenter: parent.verticalCenter }
        }
    }
}
