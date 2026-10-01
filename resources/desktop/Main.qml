import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: window
    objectName: "quest3dWindow"
    width: 870
    height: 692
    minimumWidth: 740
    minimumHeight: 590
    visible: true
    title: "Sterevi"
    color: style.background
    flags: Qt.Window | Qt.FramelessWindowHint
    font.family: style.family
    property int page: 0
    readonly property var state: bridge.state
    readonly property color hintColor: style.muted
    property string previousError: ""
    onStateChanged: {
        var currentError = String(window.state.error || "")
        if (currentError && currentError !== previousError)
            Qt.callLater(function() { if (body.contentItem) body.contentItem.contentY = 0 })
        previousError = currentError
    }
    readonly property bool working: !!window.state.busy || !!window.state.commandPending
    readonly property string statusLabel: window.state.phase === "checking" ? "확인 중" :
        window.state.phase === "starting" ? "준비 중" : window.state.phase === "stopping" ? "중지 중" :
        window.state.phase === "conflict" || window.state.error ? "확인 필요" :
        window.state.host_running && window.state.ready ? "송출 중" : window.state.running && window.state.ready ? "화면 준비됨" : "대기 중"
    readonly property color statusColor: window.state.error || window.state.phase === "conflict" ? style.error :
        window.state.host_running && window.state.ready ? style.success : style.muted
    readonly property real actualDepth: Number(window.state.depth_percent || 0)
    readonly property var monitorList: window.state.monitors || []
    readonly property var outputProfiles: window.state.output_profiles || []
    readonly property var aiQualities: window.state.ai_qualities || []
    readonly property var depthModels: window.state.depth_models && typeof window.state.depth_models !== "string" && typeof window.state.depth_models.length === "number" ? window.state.depth_models : []
    readonly property string sourceAddress: window.state.pc_address || "주소 확인 전"
    QStyle { id: style }
    function selectPage(value) { page = value; body.contentItem.contentY = 0 }
    function monitorIndex() {
        for (var i = 0; i < monitorList.length; i++)
            if (monitorList[i].device_name === window.state.selected_monitor) return i
        return -1
    }
    function monitorTitle(row) { return row ? "Monitor " + row.index : "모니터 확인 전" }
    function outputProfileIndex() {
        for (var i = 0; i < outputProfiles.length; i++)
            if (outputProfiles[i].id === window.state.output_profile) return i
        return -1
    }
    function depthModelIndex() {
        for (var i = 0; i < depthModels.length; i++)
            if (depthModels[i] && depthModels[i].id === window.state.depth_model) return i
        return -1
    }
    function aiQualityIndex() {
        for (var i = 0; i < aiQualities.length; i++)
            if (aiQualities[i].id === window.state.ai_quality) return i
        return -1
    }
    function depthModelLabel(row) {
        return row && row.label ? row.label + (row.available === true ? "" : " · 미설치") : "모델 확인 전"
    }
    function monitorSize(row) { return row && row.width && row.height ? row.width + " × " + row.height : "" }
    function eyeSize() {
        var match = String(window.state.eye_text || "").match(/(\d+)\s*[×x]\s*(\d+)/)
        return match ? match[1] + " × " + match[2] : "확인 전"
    }
    function revealFocus() {
        var target = activeFocusItem
        var ancestor = target
        while (ancestor && ancestor !== contents) ancestor = ancestor.parent
        if (!target || ancestor !== contents || !body.contentItem) return
        var point = target.mapToItem(contents, 0, 0)
        var scroll = body.contentItem
        var next = scroll.contentY
        if (point.y < next + 6) next = point.y - 6
        else if (point.y + target.height > next + body.availableHeight - 6)
            next = point.y + target.height - body.availableHeight + 6
        scroll.contentY = Math.max(0, Math.min(next, Math.max(0, contents.height - body.availableHeight)))
    }
    onActiveFocusItemChanged: Qt.callLater(revealFocus)
    onClosing: function(event) { if (!lifecycle.allowClose) { event.accepted = false; lifecycle.requestClose() } }

    background: Rectangle {
        color: style.background; border.color: "#CDD1D7"; radius: window.visibility === Window.Maximized ? 0 : 11
        Rectangle { x: 1; y: 1; width: 180; height: parent.height - 2; radius: window.visibility === Window.Maximized ? 0 : 10; color: "#F6F6F8" }
    }

    // Native system move/resize retains Windows window management without a second title bar.
    Item {
        id: titleBar
        height: 72
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        MouseArea { anchors.fill: parent; onPressed: window.startSystemMove(); onDoubleClicked: window.visibility === Window.Maximized ? window.showNormal() : window.showMaximized() }
        Row {
            x: 20; anchors.verticalCenter: parent.verticalCenter; spacing: 2
            Image { source: "../ui/brand/quest3d-mark.png"; width: 40; height: 40; fillMode: Image.PreserveAspectFit; sourceSize.width: 80; sourceSize.height: 80; anchors.verticalCenter: parent.verticalCenter }
            QText { text: "Sterevi"; font.pixelSize: 23; font.weight: Font.DemiBold }
        }
        Row {
            anchors.right: parent.right; anchors.rightMargin: 12; y: 8; spacing: 2
            QButton { objectName: "minimizeButton"; width: 46; height: 36; tone: "quiet"; iconName: "minus"; Accessible.name: "최소화"; onClicked: window.showMinimized() }
            QButton { objectName: "maximizeButton"; width: 46; height: 36; tone: "quiet"; iconName: "app-window"; Accessible.name: "창 크기"; onClicked: window.visibility === Window.Maximized ? window.showNormal() : window.showMaximized() }
            QButton { objectName: "closeButton"; width: 46; height: 36; tone: "quiet"; iconName: "x"; Accessible.name: "닫기"; onClicked: lifecycle.requestClose() }
        }
    }

    Item {
        id: sidebar
        width: 194
        anchors.top: titleBar.bottom; anchors.bottom: parent.bottom; anchors.bottomMargin: 20
        Column {
            x: 10; y: 19; spacing: 3
            Repeater {
                model: [{label: "Display", icon: "monitor"}, {label: "Connection", icon: "wifi"}, {label: "Settings", icon: "settings-2"}]
                QButton {
                    required property var modelData
                    required property int index
                    objectName: "nav" + index
                    width: 164; height: 56; tone: "nav"; centered: false
                    text: modelData.label; iconName: modelData.icon; labelSize: 17
                    selected: window.page === index
                    onClicked: window.selectPage(index)
                }
            }
        }
        QButton { objectName: "navHelp"; x: 10; anchors.bottom: parent.bottom; width: 164; height: 56; text: "Help"; iconName: "circle-help"; labelSize: 17; tone: "nav"; centered: false; selected: window.page === 3; onClicked: window.selectPage(3) }
    }

    Item {
        id: main
        anchors.left: sidebar.right; anchors.leftMargin: 22
        anchors.right: parent.right; anchors.rightMargin: 34
        anchors.top: titleBar.bottom; anchors.topMargin: 7
        anchors.bottom: parent.bottom; anchors.bottomMargin: 22
        RowLayout {
            id: heading
            width: parent.width; height: 50
            QText { text: ["Display", "Connection", "Settings", "Help"][window.page]; font.pixelSize: 28; font.weight: Font.DemiBold; Layout.fillWidth: true }
            Rectangle { width: 12; height: 12; radius: 6; color: window.statusColor; Layout.rightMargin: 5 }
            QText { objectName: "statusLabel"; text: window.statusLabel; color: window.statusColor; font.pixelSize: 16 }
        }

        ScrollView {
            id: body
            objectName: "pageScroll"
            anchors.top: heading.bottom; anchors.topMargin: 15
            anchors.left: parent.left; anchors.right: parent.right
            anchors.bottom: footer.top; anchors.bottomMargin: 10
            clip: true
            contentWidth: availableWidth
            contentHeight: contents.implicitHeight
            rightPadding: contentHeight > height + 1 ? 12 : 0
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical: ScrollBar {
                objectName: "pageScrollBar"
                parent: body
                anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.right: parent.right
                orientation: Qt.Vertical
                policy: ScrollBar.AsNeeded; width: 7; active: true
                contentItem: Rectangle { implicitWidth: 5; radius: 3; color: parent.pressed ? style.muted : "#BEC4CB" }
                background: Item {}
            }

            Column {
                id: contents
                width: body.availableWidth
                spacing: 16

                // Failures remain visible on every page; technical detail stays in the disclosure.
                Rectangle {
                    objectName: "errorPanel"
                    width: parent.width; height: errorColumn.implicitHeight + 24
                    visible: !!window.state.error || window.state.phase === "conflict"
                    radius: 8; color: "#FBEFEB"
                    Column {
                        id: errorColumn; x: 14; y: 12; width: parent.width - 28; spacing: 8
                        RowLayout { width: parent.width
                            QText { text: "PC 상태 확인 필요"; color: style.error; font.weight: Font.Medium; Layout.fillWidth: true }
                            QButton { objectName: "errorDetailButton"; height: 32; text: errorDetails.visible ? "접기" : "원인"; tone: "quiet"; onClicked: errorDetails.visible = !errorDetails.visible }
                        }
                        QText { id: errorDetails; objectName: "errorDetail"; visible: false; width: parent.width; text: String(window.state.error || window.state.message || "").replace(/[.!。]+$/, ""); font.pixelSize: 13; wrapMode: Text.Wrap; color: style.error }
                    }
                }

                Column {
                    id: screenPage; objectName: "screenPage"; visible: window.page === 0; width: parent.width; spacing: 0
                    QText { text: "Monitor"; color: style.muted; font.pixelSize: 17; height: 31 }
                    ComboBox {
                        id: monitors; objectName: "monitorCombo"; width: parent.width; height: 56
                        model: window.monitorList
                        currentIndex: window.monitorIndex()
                        enabled: !!window.state.canSelectMonitor
                        font.family: style.family; font.pixelSize: 16
                        Accessible.name: "Monitor"
                        onActivated: function(index) { bridge.selectMonitor(window.monitorList[index].device_name) }
                        contentItem: RowLayout {
                            spacing: 16
                            QSymbol { name: "monitor"; size: 28; Layout.leftMargin: 20 }
                            QText { text: window.monitorTitle(monitors.currentIndex >= 0 ? window.monitorList[monitors.currentIndex] : null); font.pixelSize: 17; Layout.fillWidth: true; elide: Text.ElideRight }
                            QText { text: window.monitorSize(monitors.currentIndex >= 0 ? window.monitorList[monitors.currentIndex] : null); font.pixelSize: 16; color: style.muted; Layout.rightMargin: 50 }
                        }
                        indicator: QSymbol { name: "chevron-down"; x: parent.width - 36; y: 17; size: 20; tint: monitors.enabled ? style.text : style.faint }
                        background: Rectangle { radius: 9; color: monitors.hovered && monitors.enabled ? "#F4F5F6" : style.background; border.width: monitors.activeFocus ? 2 : 1; border.color: monitors.activeFocus ? style.accent : "#CDD2D9" }
                        delegate: ItemDelegate {
                            id: monitorDelegate; required property var modelData; required property int index
                            width: monitors.width; height: 50
                            contentItem: QText { text: window.monitorTitle(monitorDelegate.modelData) + "  ·  " + window.monitorSize(monitorDelegate.modelData); elide: Text.ElideRight }
                            background: Rectangle { color: monitorDelegate.highlighted ? style.accentFill : style.background; radius: 7 }
                            highlighted: monitors.highlightedIndex === index
                        }
                        popup: Popup { y: monitors.height + 4; width: monitors.width; padding: 5; implicitHeight: Math.min(260, monitorChoices.contentHeight + 10)
                            background: Rectangle { radius: 10; color: style.background; border.color: style.line }
                            contentItem: ListView { id: monitorChoices; clip: true; implicitHeight: contentHeight; model: monitors.popup.visible ? monitors.delegateModel : null; currentIndex: monitors.highlightedIndex; ScrollIndicator.vertical: ScrollIndicator {} }
                        }
                        ToolTip.visible: hovered && !enabled
                        ToolTip.text: "모니터 변경: PC 중지 후 가능"
                    }
                    Item { width: 1; height: 18 }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    Item { width: parent.width; height: 82
                        QText { text: "Mode"; font.pixelSize: 17; font.weight: Font.Medium; anchors.verticalCenter: parent.verticalCenter }
                        Rectangle {
                            width: 226; height: 46; radius: 9; color: "#EEEFF1"; border.color: "#E0E2E6"; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                            Row { anchors.fill: parent; spacing: 0
                                Repeater { model: ["2D", "3D"]
                                    QButton {
                                        required property string modelData
                                        objectName: "mode" + modelData
                                        width: 113; height: 46; text: modelData; labelSize: 17; enabled: !!window.state.canControl
                                        selected: window.state.mode === modelData.toLowerCase()
                                        background: Rectangle { radius: 9; color: parent.selected ? "#FFFFFF" : parent.hovered ? "#E4E6EA" : "transparent"; border.width: parent.activeFocus ? 2 : parent.selected ? 1 : 0; border.color: parent.activeFocus ? style.accent : "#D0D4DA" }
                                        onClicked: bridge.setMode(modelData.toLowerCase())
                                    }
                                }
                            }
                        }
                    }
                    Item { width: parent.width; height: 113
                        QText { text: "Depth"; font.pixelSize: 17; font.weight: Font.Medium; y: 25 }
                        Column {
                            anchors.right: parent.right; y: 10; width: Math.min(300, parent.width * .53); spacing: 10
                            RowLayout { width: 226; anchors.right: parent.right; spacing: 12
                                QButton { objectName: "depthMinus"; Layout.preferredWidth: 46; Layout.minimumWidth: 46; height: 46; iconName: "minus"; Accessible.name: "Depth 감소"; enabled: !!window.state.canControl && window.actualDepth > 0; onClicked: bridge.adjustDepth(-.05) }
                                QField { id: depthInput; objectName: "depthInput"; Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; font.pixelSize: 22; font.weight: Font.DemiBold; enabled: !!window.state.canControl; inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    text: window.actualDepth.toFixed(2) + "%"; Accessible.name: "Depth 퍼센트"
                                    background: Rectangle { color: "transparent"; radius: 7; border.width: depthInput.activeFocus ? 2 : 0; border.color: style.accent }
                                    validator: RegularExpressionValidator { regularExpression: /(?:[0-3](?:\.[0-9]{0,4})?|4(?:\.0{0,4})?)%?/ }
                                    onEditingFinished: { if (acceptableInput && text !== window.actualDepth.toFixed(2) + "%") bridge.setDepth(Number(text.replace("%", ""))); text = Qt.binding(function() { return window.actualDepth.toFixed(2) + "%" }) }
                                }
                                QButton { objectName: "depthPlus"; Layout.preferredWidth: 46; Layout.minimumWidth: 46; height: 46; iconName: "plus"; Accessible.name: "Depth 증가"; enabled: !!window.state.canControl && window.actualDepth < 4; onClicked: bridge.adjustDepth(.05) }
                            }
                            Slider {
                                id: depthSlider; objectName: "depthSlider"; width: parent.width; height: 30; from: 0; to: 4; stepSize: .05; enabled: !!window.state.canControl
                                property bool pointerEditing: false
                                value: window.actualDepth; live: true; Accessible.name: "Depth"
                                onPressedChanged: {
                                    if (pressed) pointerEditing = true
                                    else if (pointerEditing) { pointerEditing = false; if (enabled && Math.abs(value - window.actualDepth) > .0001) bridge.setDepth(value); value = Qt.binding(function() { return window.actualDepth }) }
                                }
                                Keys.onReleased: function(event) { if ([Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End].indexOf(event.key) >= 0 && enabled) { bridge.setDepth(value); value = Qt.binding(function() { return window.actualDepth }); event.accepted = true } }
                                background: Rectangle { x: 0; y: (parent.height - height) / 2; width: parent.width; height: 8; radius: 4; color: "#DADCE0"
                                    Rectangle { width: depthSlider.visualPosition * parent.width; height: parent.height; radius: 4; color: depthSlider.enabled ? style.slider : "#BCC1C8" }
                                }
                                handle: Rectangle { x: depthSlider.visualPosition * (depthSlider.width - width); y: (depthSlider.height - height) / 2; width: 27; height: 27; radius: 14; color: "white"; border.width: depthSlider.activeFocus ? 2 : 1; border.color: depthSlider.activeFocus ? style.accent : "#D5D9DF" }
                            }
                        }
                    }
                    Item { width: parent.width; height: Math.max(70, comfortLabels.implicitHeight + 20)
                        Column {
                            id: comfortLabels
                            width: parent.width - comfort.width - 24
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 5
                            QText { text: "윤곽 안정화"; font.pixelSize: 17; font.weight: Font.Medium }
                            QText { objectName: "comfortDescription"; width: parent.width; text: "양안 차이를 줄여 외곽선 겹침 완화"; font.pixelSize: 13; color: style.muted; wrapMode: Text.Wrap }
                        }
                        Switch { id: comfort; objectName: "comfortSwitch"; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; width: 56; height: 38; padding: 0; checked: window.state.profile === "comfort"; enabled: !!window.state.canControl; Accessible.name: "윤곽 안정화"
                            onClicked: { bridge.setComfort(checked); checked = Qt.binding(function() { return window.state.profile === "comfort" }) }
                            indicator: Rectangle { y: 4; width: 56; height: 31; radius: 16; color: comfort.checked ? (comfort.enabled ? style.slider : "#C8BBB7") : "#D3D7DC"; border.width: comfort.activeFocus ? 2 : 0; border.color: style.accent
                                Rectangle { x: comfort.checked ? 27 : 3; y: 3; width: 25; height: 25; radius: 13; color: "white"; Behavior on x { NumberAnimation { duration: 100 } } }
                            }
                        }
                    }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    Item { width: parent.width; height: 79
                        QText { text: "Eye Resolution"; font.pixelSize: 17; font.weight: Font.Medium; anchors.verticalCenter: parent.verticalCenter }
                        QText { objectName: "eyeResolution"; text: window.eyeSize(); font.pixelSize: 17; width: 320; horizontalAlignment: Text.AlignHCenter; anchors.right: parent.right; anchors.rightMargin: 108; anchors.verticalCenter: parent.verticalCenter }
                    }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    RowLayout { width: parent.width; height: 76; spacing: 16
                        Column { Layout.fillWidth: true; spacing: 5
                            QText { text: "Quest view"; font.pixelSize: 17; font.weight: Font.Medium }
                            QText { width: parent.width; text: "화면 크기·위치·보기 저장 · Quest Display"; color: style.muted; font.pixelSize: 13; wrapMode: Text.Wrap }
                        }
                        QButton { objectName: "questViewGuide"; text: "화면 설정"; iconName: "monitor"; Accessible.name: "Quest 화면 설정 안내"; onClicked: { window.page = 3; questViewHelp.forceActiveFocus() } }
                    }
                }

                Column {
                    objectName: "connectionPage"; visible: window.page === 1; width: parent.width; spacing: 18
                    Column { width: parent.width; spacing: 12
                        QText { text: "PC 주소"; font.pixelSize: 17; color: style.muted }
                        QField { objectName: "pcAddress"; width: parent.width; readOnly: true; text: window.sourceAddress; Accessible.name: "PC 주소" }
                        QText { text: window.state.connected === true ? "Quest 연결됨" : window.state.connected === false ? "Quest 연결 대기" : "Quest 연결 확인 전"; color: window.state.connected === true ? style.success : style.muted }
                    }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    Column { width: parent.width; spacing: 14
                        QText { text: "새 Quest 연결"; font.pixelSize: 17; font.weight: Font.Medium }
                        QText { width: parent.width; text: "Quest: Select Server → + → PC 주소 → Pair"; color: style.muted; wrapMode: Text.Wrap }
                        RowLayout { width: parent.width; spacing: 12
                            QField { id: pin; objectName: "pairPin"; Layout.fillWidth: true; placeholderText: "4자리 PIN"; echoMode: TextInput.Password; maximumLength: 4; inputMethodHints: Qt.ImhDigitsOnly | Qt.ImhHiddenText | Qt.ImhNoPredictiveText; validator: RegularExpressionValidator { regularExpression: /[0-9]{4}/ } enabled: !!window.state.canPair; Accessible.name: "Quest PIN"; onAccepted: pairButton.clicked() }
                            QButton { id: pairButton; objectName: "pairButton"; text: "연결 승인"; tone: "primary"; enabled: !!window.state.canPair && pin.acceptableInput; onClicked: { if (bridge.pair(pin.text)) pin.clear() } }
                        }
                        QText { visible: !window.state.host_running; width: parent.width; text: "연결 전 PC 시작 필요"; color: style.muted; font.pixelSize: 13 }
                        QText { objectName: "pairingMessage"; visible: !!window.state.pairing_message; width: parent.width; text: String(window.state.pairing_message || "").replace(/[.!。]+$/, ""); wrapMode: Text.Wrap; color: style.muted; font.pixelSize: 13 }
                    }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    Column { width: parent.width; spacing: 12
                        QText { text: "처음 사용하는 PC"; font.pixelSize: 17; font.weight: Font.Medium }
                        QText { width: parent.width; text: "PC 시작 후 Quest에서 Connect\n같은 Wi-Fi·유선망 사용 · 최초 설치 시 방화벽 허용"; color: style.muted; lineHeight: 1.5; wrapMode: Text.Wrap }
                        QButton { text: "설치 안내"; iconName: "external-link"; enabled: !!window.state.canUtility; onClicked: bridge.openGuide() }
                    }
                }

                Column {
                    objectName: "settingsPage"; visible: window.page === 2; width: parent.width; spacing: 20
                    Column { width: parent.width; spacing: 12
                        QText { text: "Sound"; font.pixelSize: 17; font.weight: Font.Medium }
                        RowLayout { width: parent.width; spacing: 8
                            Repeater {
                                model: window.state.audio_outputs || []
                                QButton {
                                    required property var modelData
                                    objectName: "audioOutput_" + modelData.id
                                    text: modelData.label; Layout.fillWidth: true; Layout.preferredWidth: 1
                                    selected: window.state.audio_output === modelData.id
                                    enabled: !!window.state.canSelectAudioOutput && modelData.available === true
                                    onClicked: bridge.selectAudioOutput(modelData.id)
                                }
                            }
                        }
                        QText { objectName: "audioOutputHint"; width: parent.width; font.pixelSize: 13; color: style.muted; wrapMode: Text.Wrap
                            text: (window.state.audio_output === "quest" ? "Quest 전용 · 연결 종료 시 PC 출력 복원\n음량 조절 · Quest 볼륨 버튼" :
                                   window.state.audio_output === "both" ? "PC와 Quest 동시 재생 · 양쪽 음량에 따라 소리 겹침 가능" : "기존 PC 스피커·헤드폰 사용") +
                                  ((window.state.running || window.state.host_running) ? "\n출력 변경 전 PC 송출 중지" : "")
                        }
                        QText { objectName: "audioOutputStatus"; width: parent.width; text: window.state.audio_status || "출력 장치 확인 중"; font.pixelSize: 12; color: style.muted; wrapMode: Text.Wrap }
                        QText { objectName: "audioError"; visible: !!window.state.audio_error; width: parent.width; text: window.state.audio_error || ""; font.pixelSize: 13; color: style.error; wrapMode: Text.Wrap }
                        Repeater {
                            model: window.state.audio_outputs || []
                            QText { required property var modelData; visible: modelData.id === "quest" && !modelData.available && !window.state.host_running
                                width: parent.width; text: modelData.reason || ""; font.pixelSize: 12; color: window.hintColor; wrapMode: Text.Wrap }
                        }
                    }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    QText { text: "Quality"; font.pixelSize: 17; font.weight: Font.Medium }
                    QText { width: parent.width; text: window.state.quality_text || "화질 정보 확인 전"; color: style.muted; wrapMode: Text.Wrap }
                    RowLayout { width: parent.width; spacing: 20
                        QText { text: "Headset"; font.pixelSize: 15; Layout.fillWidth: true }
                        ComboBox {
                            id: headset; objectName: "headsetCombo"; Layout.preferredWidth: 210; Layout.preferredHeight: 42
                            model: window.outputProfiles; textRole: "label"
                            currentIndex: window.outputProfileIndex()
                            enabled: !!window.state.canSelectOutputProfile
                            Accessible.name: "Headset 출력 해상도"
                            onActivated: function(index) { bridge.selectOutputProfile(window.outputProfiles[index].id) }
                            contentItem: QText { text: headset.displayText; leftPadding: 14; rightPadding: 36; verticalAlignment: Text.AlignVCenter; color: headset.enabled ? style.text : style.muted }
                            indicator: QSymbol { name: "chevron-down"; x: parent.width - 31; y: 11; size: 20; tint: headset.enabled ? style.text : style.faint }
                            background: Rectangle { radius: 9; color: headset.hovered && headset.enabled ? "#F4F5F6" : style.background; border.width: headset.activeFocus ? 2 : 1; border.color: headset.activeFocus ? style.accent : "#CDD2D9" }
                            delegate: ItemDelegate {
                                id: headsetDelegate; required property var modelData; required property int index
                                width: headset.width; height: 40
                                contentItem: QText { text: headsetDelegate.modelData.label; verticalAlignment: Text.AlignVCenter }
                                background: Rectangle { color: headsetDelegate.highlighted ? style.accentFill : style.background; radius: 7 }
                                highlighted: headset.highlightedIndex === index
                            }
                            popup: Popup { y: headset.height + 4; width: headset.width; padding: 5; implicitHeight: headsetChoices.contentHeight + 10
                                background: Rectangle { radius: 9; color: style.background; border.color: style.line }
                                contentItem: ListView { id: headsetChoices; clip: true; implicitHeight: contentHeight; model: headset.popup.visible ? headset.delegateModel : null; currentIndex: headset.highlightedIndex }
                            }
                        }
                    }
                    QText { visible: !window.state.canSelectOutputProfile; width: parent.width; text: "Headset 변경 전 PC 송출 중지"; color: style.muted; font.pixelSize: 13 }
                    QText { width: parent.width; text: window.state.eye_text || "눈별 해상도 확인 전"; wrapMode: Text.Wrap }
                    Column { width: parent.width; spacing: 8
                        RowLayout { width: parent.width; spacing: 20
                            QText { text: "AI Quality"; font.pixelSize: 15; Layout.fillWidth: true }
                            ComboBox {
                                id: aiQuality; objectName: "aiQualityCombo"; Layout.preferredWidth: 230; Layout.preferredHeight: 42
                                model: window.aiQualities; textRole: "label"
                                currentIndex: window.aiQualityIndex()
                                enabled: !!window.state.canSelectAiQuality
                                Accessible.name: "AI Quality 깊이 분석 품질"
                                onActivated: function(index) {
                                    var row = window.aiQualities[index]
                                    if (row) bridge.selectAiQuality(row.id)
                                    currentIndex = Qt.binding(function() { return window.aiQualityIndex() })
                                }
                                contentItem: QText { objectName: "aiQualityLabel"; text: (window.state.running && !window.state.active_ai_quality ? "재시작 시 적용 · " : "") + aiQuality.displayText; leftPadding: 14; rightPadding: 36; verticalAlignment: Text.AlignVCenter; color: aiQuality.enabled ? style.text : style.muted }
                                indicator: QSymbol { name: "chevron-down"; x: parent.width - 31; y: 11; size: 20; tint: aiQuality.enabled ? style.text : style.faint }
                                background: Rectangle { radius: 9; color: aiQuality.hovered && aiQuality.enabled ? "#F4F5F6" : style.background; border.width: aiQuality.activeFocus ? 2 : 1; border.color: aiQuality.activeFocus ? style.accent : "#CDD2D9" }
                                delegate: ItemDelegate {
                                    id: aiQualityDelegate; required property var modelData; required property int index
                                    objectName: "aiQualityOption" + index
                                    width: aiQuality.width; height: 40
                                    contentItem: QText { text: aiQualityDelegate.modelData.label; verticalAlignment: Text.AlignVCenter }
                                    background: Rectangle { color: aiQualityDelegate.highlighted ? style.accentFill : style.background; radius: 7 }
                                    highlighted: aiQuality.highlightedIndex === index
                                }
                                popup: Popup { y: aiQuality.height + 4; width: aiQuality.width; padding: 5; implicitHeight: aiQualityChoices.contentHeight + 10
                                    background: Rectangle { radius: 9; color: style.background; border.color: style.line }
                                    contentItem: ListView { id: aiQualityChoices; clip: true; implicitHeight: contentHeight; model: aiQuality.popup.visible ? aiQuality.delegateModel : null; currentIndex: aiQuality.highlightedIndex }
                                }
                            }
                        }
                        QText { objectName: "aiQualityHint"; width: parent.width; text: (window.state.ai_quality === "quality" ? "세밀한 깊이 분석 · 장면에 따라 입체감 변화 · 처리 속도 감소" : "빠른 깊이 분석") + (!window.state.canSelectAiQuality ? "\n변경하려면 PC 중지" : ""); color: style.muted; font.pixelSize: 13; wrapMode: Text.Wrap }
                        QText { objectName: "aiInputSize"; width: parent.width; text: window.state.ai_input_text || "실제 AI 입력 확인 전"; color: style.muted; font.pixelSize: 13; wrapMode: Text.Wrap }
                    }
                    Column { width: parent.width; spacing: 8
                        RowLayout { width: parent.width; spacing: 20
                            QText { text: "Depth model"; font.pixelSize: 15; Layout.fillWidth: true }
                            ComboBox {
                                id: depthModel; objectName: "depthModelCombo"; Layout.preferredWidth: 230; Layout.preferredHeight: 42
                                model: window.depthModels; textRole: "label"
                                currentIndex: window.depthModelIndex()
                                displayText: window.depthModelLabel(window.depthModels[window.depthModelIndex()])
                                enabled: !!window.state.canSelectDepthModel
                                Accessible.name: "Depth model 깊이 모델"
                                onActivated: function(index) {
                                    var row = window.depthModels[index]
                                    if (row && row.available === true) bridge.selectDepthModel(row.id)
                                    currentIndex = Qt.binding(function() { return window.depthModelIndex() })
                                }
                                contentItem: QText { objectName: "depthModelLabel"; text: depthModel.displayText; leftPadding: 14; rightPadding: 36; verticalAlignment: Text.AlignVCenter; color: depthModel.enabled ? style.text : style.muted }
                                indicator: QSymbol { name: "chevron-down"; x: parent.width - 31; y: 11; size: 20; tint: depthModel.enabled ? style.text : style.faint }
                                background: Rectangle { radius: 9; color: depthModel.hovered && depthModel.enabled ? "#F4F5F6" : style.background; border.width: depthModel.activeFocus ? 2 : 1; border.color: depthModel.activeFocus ? style.accent : "#CDD2D9" }
                                delegate: ItemDelegate {
                                    id: depthModelDelegate; required property var modelData; required property int index
                                    objectName: "depthModelOption" + index
                                    width: depthModel.width; height: 40
                                    enabled: !!modelData && modelData.available === true
                                    readonly property color labelColor: enabled ? style.text : style.muted
                                    contentItem: QText { text: window.depthModelLabel(depthModelDelegate.modelData); verticalAlignment: Text.AlignVCenter; color: depthModelDelegate.labelColor }
                                    background: Rectangle { color: depthModelDelegate.highlighted && depthModelDelegate.enabled ? style.accentFill : style.background; radius: 7 }
                                    highlighted: depthModel.highlightedIndex === index
                                }
                                popup: Popup { y: depthModel.height + 4; width: depthModel.width; padding: 5; implicitHeight: depthModelChoices.contentHeight + 10
                                    background: Rectangle { radius: 9; color: style.background; border.color: style.line }
                                    contentItem: ListView { id: depthModelChoices; clip: true; implicitHeight: contentHeight; model: depthModel.popup.visible ? depthModel.delegateModel : null; currentIndex: depthModel.highlightedIndex }
                                }
                            }
                        }
                        QText { width: parent.width; text: "장면별 윤곽·입체감 비교"; color: style.muted; font.pixelSize: 13; wrapMode: Text.Wrap }
                        QText { objectName: "depthModelHint"; width: parent.width; text: window.state.depth_model_switching ? "모델 전환 중" : "DAD: 장면에 따라 입체감 감소"; color: style.muted; font.pixelSize: 13; wrapMode: Text.Wrap }
                    }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    RowLayout { width: parent.width
                        QText { text: "진단"; font.pixelSize: 17; font.weight: Font.Medium; Layout.fillWidth: true }
                        QButton { objectName: "refreshButton"; text: "상태 새로고침"; enabled: !!window.state.canUtility; onClicked: bridge.refresh() }
                    }
                    QText { objectName: "metrics"; width: parent.width; text: window.state.metrics_text || "측정 전"; color: style.muted; wrapMode: Text.Wrap }
                    Flow { width: parent.width; spacing: 10
                        QButton { objectName: "exportButton"; text: "진단 내보내기"; enabled: !!window.state.canUtility; onClicked: bridge.exportDiagnostics() }
                        QButton { objectName: "logsButton"; text: "로그 폴더"; enabled: !!window.state.canUtility; onClicked: bridge.openLogs() }
                        QButton { objectName: "diagnosticsButton"; text: "최근 진단"; enabled: !!window.state.canOpenDiagnostics; onClicked: bridge.openDiagnostics() }
                    }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    QText { text: "App"; font.pixelSize: 17; font.weight: Font.Medium }
                    Flow { width: parent.width; spacing: 10
                        QButton { objectName: "hideButton"; text: "트레이로 보내기"; onClicked: bridge.requestHide() }
                        QButton { objectName: "exitButton"; text: "종료"; onClicked: bridge.requestExit() }
                    }
                }

                Column {
                    objectName: "helpPage"; visible: window.page === 3; width: parent.width; spacing: 16
                    QText { id: questViewHelp; objectName: "questViewHelp"; text: "Quest view"; font.pixelSize: 17; font.weight: Font.Medium; activeFocusOnTab: true }
                    QText { width: parent.width; text: "Quest Display\nSize  화면 크기 · Position  거리·이동·기울기\nViews  보기 저장 · Environment  곡률·배경\nFraming  위·아래 여백 자르기"; lineHeight: 1.65; wrapMode: Text.Wrap }
                    QText { width: parent.width; text: "Center  정면으로 이동 · Lock  화면 위치 잠금\n설정창 크기·포인터  Connect → Menu / Pointer"; color: style.muted; lineHeight: 1.6; font.pixelSize: 13; wrapMode: Text.Wrap }
                    QText { width: parent.width; text: "PC  원본·입체감·송출 품질\nQuest  가상 화면 배치·감상 환경"; color: style.muted; lineHeight: 1.6; font.pixelSize: 13; wrapMode: Text.Wrap }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    QText { text: "시작 순서"; font.pixelSize: 17; font.weight: Font.Medium }
                    QText { width: parent.width; text: "1   PC 시작\n2   Quest 앱 Connect\n3   Mode · Depth 조절"; lineHeight: 1.8 }
                    Rectangle { width: parent.width; height: 1; color: style.line }
                    QText { text: "연결 조건"; font.pixelSize: 17; font.weight: Font.Medium }
                    QText { width: parent.width; text: "PC와 Quest: 같은 사설 네트워크\n새 기기: Connection 탭에서 PIN 승인"; color: style.muted; lineHeight: 1.6; wrapMode: Text.Wrap }
                    QText { text: "화면 제한"; font.pixelSize: 17; font.weight: Font.Medium }
                    QText { width: parent.width; text: "보호된 영상은 검은 화면으로 표시될 수 있음\n모니터 변경은 PC 중지 후 가능"; color: style.muted; lineHeight: 1.6; wrapMode: Text.Wrap }
                    QButton { objectName: "guideButton"; text: "사용 안내"; iconName: "external-link"; enabled: !!window.state.canUtility; onClicked: bridge.openGuide() }
                }
            }
        }

        Column {
            id: footer; width: parent.width; anchors.bottom: parent.bottom; spacing: 12
            Rectangle { width: parent.width; height: 1; color: style.line }
            RowLayout { width: parent.width; height: 52
                Column { Layout.fillWidth: true; spacing: 5
                    QText { text: window.sourceAddress; color: style.muted; font.pixelSize: 14 }
                    QText { objectName: "notice"; text: bridge.notice; visible: text !== ""; color: bridge.noticeKind === "error" ? style.error : style.muted; font.pixelSize: 13; width: Math.max(80, main.width - primary.width - 20); wrapMode: Text.Wrap }
                }
                QButton { id: primary; objectName: "primaryButton"; text: window.working ? "처리 중" : window.state.primaryAction === "stop" ? "PC 중지" : "PC 시작"; iconName: "power"; labelSize: 17; enabled: window.state.primaryAction === "stop" ? !!window.state.canStop : !!window.state.canStart; tone: window.state.primaryAction === "start" ? "primary" : "secondary"; width: 148; height: 50; onClicked: bridge.primary() }
            }
        }
    }

    Dialog {
        id: closeDialog; objectName: "closeDialog"; visible: lifecycle.closePrompt
        parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(490, window.width - 40)
        modal: true; closePolicy: Popup.NoAutoClose; padding: 24
        background: Rectangle { radius: 13; color: style.background; border.color: style.line }
        contentItem: Column { spacing: 20
            QText { text: "송출 중"; font.pixelSize: 21; font.weight: Font.DemiBold }
            QText { width: parent.width; text: "트레이: 송출 유지\n중지 후 종료: Quest 연결 종료"; color: style.muted; lineHeight: 1.6 }
            Flow { width: parent.width; spacing: 8
                QButton { objectName: "closeHideButton"; text: "트레이"; onClicked: lifecycle.hide() }
                QButton { objectName: "closeStopButton"; text: "중지 후 종료"; onClicked: lifecycle.stopAndExit() }
                QButton { objectName: "closeCancelButton"; text: "취소"; onClicked: lifecycle.cancelClose() }
            }
        }
        Overlay.modal: Rectangle { color: "#440F1722" }
    }

    Repeater {
        model: [{e: Qt.LeftEdge, x: 0, y: 6, w: 6, h: window.height - 12, c: Qt.SizeHorCursor},
                {e: Qt.RightEdge, x: window.width - 6, y: 6, w: 6, h: window.height - 12, c: Qt.SizeHorCursor},
                {e: Qt.TopEdge, x: 6, y: 0, w: window.width - 12, h: 6, c: Qt.SizeVerCursor},
                {e: Qt.BottomEdge, x: 6, y: window.height - 6, w: window.width - 12, h: 6, c: Qt.SizeVerCursor},
                {e: Qt.TopEdge | Qt.LeftEdge, x: 0, y: 0, w: 8, h: 8, c: Qt.SizeFDiagCursor},
                {e: Qt.BottomEdge | Qt.RightEdge, x: window.width - 8, y: window.height - 8, w: 8, h: 8, c: Qt.SizeFDiagCursor}]
        MouseArea { required property var modelData; x: modelData.x; y: modelData.y; width: modelData.w; height: modelData.h; cursorShape: modelData.c; enabled: window.visibility !== Window.Maximized; onPressed: window.startSystemResize(modelData.e) }
    }
}
