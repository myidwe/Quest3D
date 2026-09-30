import QtQuick
import QtQuick.Controls

TextField {
    id: field
    QStyle { id: style }
    implicitHeight: 44
    font.family: style.family
    font.pixelSize: 15
    color: enabled ? style.text : style.faint
    selectionColor: style.accentFill
    selectedTextColor: style.text
    placeholderTextColor: style.faint
    leftPadding: 12
    rightPadding: 12
    selectByMouse: true
    background: Rectangle { color: style.background; radius: 8; border.width: field.activeFocus ? 2 : 1; border.color: field.activeFocus ? style.accent : style.line }
}
