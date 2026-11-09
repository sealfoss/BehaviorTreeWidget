# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'ListEntry.ui'
##
## Created by: Qt User Interface Compiler version 6.11.2
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame,
    QHBoxLayout, QLineEdit, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget)

class Ui_List(object):
    def setupUi(self, List):
        if not List.objectName():
            List.setObjectName(u"List")
        List.resize(450, 30)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(List.sizePolicy().hasHeightForWidth())
        List.setSizePolicy(sizePolicy)
        List.setMinimumSize(QSize(0, 30))
        self.horizontalLayout = QHBoxLayout(List)
        self.horizontalLayout.setObjectName(u"horizontalLayout")
        self.horizontalLayout.setContentsMargins(0, 0, 0, 0)
        self.FrameSelection = QFrame(List)
        self.FrameSelection.setObjectName(u"FrameSelection")
        self.FrameSelection.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameSelection.setFrameShadow(QFrame.Shadow.Raised)
        self.verticalLayout_3 = QVBoxLayout(self.FrameSelection)
        self.verticalLayout_3.setObjectName(u"verticalLayout_3")
        self.verticalLayout_3.setContentsMargins(0, 0, 0, 0)
        self.Select = QCheckBox(self.FrameSelection)
        self.Select.setObjectName(u"Select")

        self.verticalLayout_3.addWidget(self.Select)


        self.horizontalLayout.addWidget(self.FrameSelection)

        self.FrameValue = QFrame(List)
        self.FrameValue.setObjectName(u"FrameValue")
        self.FrameValue.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameValue.setFrameShadow(QFrame.Shadow.Raised)
        self.horizontalLayout_5 = QHBoxLayout(self.FrameValue)
        self.horizontalLayout_5.setObjectName(u"horizontalLayout_5")
        self.horizontalLayout_5.setContentsMargins(0, 0, 0, 0)
        self.ValueName = QLineEdit(self.FrameValue)
        self.ValueName.setObjectName(u"ValueName")
        self.ValueName.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.horizontalLayout_5.addWidget(self.ValueName)

        self.Values = QComboBox(self.FrameValue)
        self.Values.setObjectName(u"Values")
        sizePolicy.setHeightForWidth(self.Values.sizePolicy().hasHeightForWidth())
        self.Values.setSizePolicy(sizePolicy)

        self.horizontalLayout_5.addWidget(self.Values)


        self.horizontalLayout.addWidget(self.FrameValue)

        self.FrameReset = QFrame(List)
        self.FrameReset.setObjectName(u"FrameReset")
        self.FrameReset.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameReset.setFrameShadow(QFrame.Shadow.Raised)
        self.layoutReset = QVBoxLayout(self.FrameReset)
        self.layoutReset.setObjectName(u"layoutReset")
        self.layoutReset.setContentsMargins(0, 0, 0, 0)
        self.Reset = QCheckBox(self.FrameReset)
        self.Reset.setObjectName(u"Reset")

        self.layoutReset.addWidget(self.Reset)


        self.horizontalLayout.addWidget(self.FrameReset)

        self.FrameOrder = QFrame(List)
        self.FrameOrder.setObjectName(u"FrameOrder")
        self.FrameOrder.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameOrder.setFrameShadow(QFrame.Shadow.Raised)
        self.layoutOrder = QHBoxLayout(self.FrameOrder)
        self.layoutOrder.setSpacing(0)
        self.layoutOrder.setObjectName(u"layoutOrder")
        self.layoutOrder.setContentsMargins(0, 0, 0, 0)
        self.MoveUp = QToolButton(self.FrameOrder)
        self.MoveUp.setObjectName(u"MoveUp")
        self.MoveUp.setArrowType(Qt.ArrowType.UpArrow)

        self.layoutOrder.addWidget(self.MoveUp)

        self.MoveDown = QToolButton(self.FrameOrder)
        self.MoveDown.setObjectName(u"MoveDown")
        self.MoveDown.setArrowType(Qt.ArrowType.DownArrow)

        self.layoutOrder.addWidget(self.MoveDown)


        self.horizontalLayout.addWidget(self.FrameOrder)


        self.retranslateUi(List)

        QMetaObject.connectSlotsByName(List)
    # setupUi

    def retranslateUi(self, List):
        List.setWindowTitle(QCoreApplication.translate("List", u"List", None))
        self.Select.setText(QCoreApplication.translate("List", u"Select", None))
        self.ValueName.setText("")
        self.ValueName.setPlaceholderText(QCoreApplication.translate("List", u"Entry Name", None))
#if QT_CONFIG(tooltip)
        self.Reset.setToolTip(QCoreApplication.translate("List", u"Resets: at the end of every execution loop, this entry goes back to the value it had when the loop started", None))
#endif // QT_CONFIG(tooltip)
        self.Reset.setText(QCoreApplication.translate("List", u"Resets", None))
#if QT_CONFIG(tooltip)
        self.MoveUp.setToolTip(QCoreApplication.translate("List", u"Move this entry up in the list", None))
#endif // QT_CONFIG(tooltip)
#if QT_CONFIG(tooltip)
        self.MoveDown.setToolTip(QCoreApplication.translate("List", u"Move this entry down in the list", None))
#endif // QT_CONFIG(tooltip)
    # retranslateUi

