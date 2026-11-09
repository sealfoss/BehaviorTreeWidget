# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'FloatEntry.ui'
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
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox, QFrame,
    QHBoxLayout, QLineEdit, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget)

class Ui_FloatEntry(object):
    def setupUi(self, FloatEntry):
        if not FloatEntry.objectName():
            FloatEntry.setObjectName(u"FloatEntry")
        FloatEntry.resize(450, 30)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(FloatEntry.sizePolicy().hasHeightForWidth())
        FloatEntry.setSizePolicy(sizePolicy)
        FloatEntry.setMinimumSize(QSize(0, 30))
        self.horizontalLayout = QHBoxLayout(FloatEntry)
        self.horizontalLayout.setObjectName(u"horizontalLayout")
        self.horizontalLayout.setContentsMargins(0, 0, 0, 0)
        self.FrameSelection = QFrame(FloatEntry)
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

        self.FrameValue = QFrame(FloatEntry)
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

        self.Value = QDoubleSpinBox(self.FrameValue)
        self.Value.setObjectName(u"Value")
        sizePolicy.setHeightForWidth(self.Value.sizePolicy().hasHeightForWidth())
        self.Value.setSizePolicy(sizePolicy)
        self.Value.setDecimals(8)
        self.Value.setMinimum(-179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368.000000000000000)
        self.Value.setMaximum(179769313486231570814527423731704356798070567525844996598917476803157260780028538760589558632766878171540458953514382464234321326889464182768467546703537516986049910576551282076245490090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738177180919299881250404026184124858368.000000000000000)

        self.horizontalLayout_5.addWidget(self.Value)


        self.horizontalLayout.addWidget(self.FrameValue)

        self.FrameReset = QFrame(FloatEntry)
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

        self.FrameOrder = QFrame(FloatEntry)
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


        self.retranslateUi(FloatEntry)

        QMetaObject.connectSlotsByName(FloatEntry)
    # setupUi

    def retranslateUi(self, FloatEntry):
        FloatEntry.setWindowTitle(QCoreApplication.translate("FloatEntry", u"Float", None))
        self.Select.setText(QCoreApplication.translate("FloatEntry", u"Select", None))
        self.ValueName.setText("")
        self.ValueName.setPlaceholderText(QCoreApplication.translate("FloatEntry", u"Entry Name", None))
#if QT_CONFIG(tooltip)
        self.Reset.setToolTip(QCoreApplication.translate("FloatEntry", u"Resets: at the end of every execution loop, this entry goes back to the value it had when the loop started", None))
#endif // QT_CONFIG(tooltip)
        self.Reset.setText(QCoreApplication.translate("FloatEntry", u"Resets", None))
#if QT_CONFIG(tooltip)
        self.MoveUp.setToolTip(QCoreApplication.translate("FloatEntry", u"Move this entry up in the list", None))
#endif // QT_CONFIG(tooltip)
#if QT_CONFIG(tooltip)
        self.MoveDown.setToolTip(QCoreApplication.translate("FloatEntry", u"Move this entry down in the list", None))
#endif // QT_CONFIG(tooltip)
    # retranslateUi

