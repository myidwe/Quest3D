import QtQuick
import QtQuick.Controls

Button {
    property string name: "monitor"
    property color tint: "#14202E"
    property int size: 22
    implicitWidth: size
    implicitHeight: size
    width: size
    height: size
    padding: 0
    icon.source: "../ui/icons/" + name + ".svg"
    icon.color: tint
    icon.width: size
    icon.height: size
    display: AbstractButton.IconOnly
    background: Item {}
    enabled: false
    focusPolicy: Qt.NoFocus
}
