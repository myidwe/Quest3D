import QtQuick

Text {
    QStyle { id: style }
    color: style.text
    font.family: style.family
    font.pixelSize: 15
    font.weight: Font.Normal
    renderType: Text.NativeRendering
    verticalAlignment: Text.AlignVCenter
}
