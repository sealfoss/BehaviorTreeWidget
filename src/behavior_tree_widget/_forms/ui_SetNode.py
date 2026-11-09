# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'SetNode.ui'
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
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout,
    QLabel, QLineEdit, QSizePolicy, QVBoxLayout,
    QWidget)

class Ui_SetNode(object):
    def setupUi(self, SetNode):
        if not SetNode.objectName():
            SetNode.setObjectName(u"SetNode")
        SetNode.resize(260, 190)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(SetNode.sizePolicy().hasHeightForWidth())
        SetNode.setSizePolicy(sizePolicy)
        self.verticalLayout_2 = QVBoxLayout(SetNode)
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.verticalLayout_2.setContentsMargins(5, 3, 5, 3)
        self.FrameParent = QFrame(SetNode)
        self.FrameParent.setObjectName(u"FrameParent")
        self.FrameParent.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameParent.setFrameShadow(QFrame.Shadow.Raised)
        self.verticalLayout_3 = QVBoxLayout(self.FrameParent)
        self.verticalLayout_3.setObjectName(u"verticalLayout_3")
        self.verticalLayout_3.setContentsMargins(5, 3, 5, 3)
        self.ParentConnection = QLabel(self.FrameParent)
        self.ParentConnection.setObjectName(u"ParentConnection")
        self.ParentConnection.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.verticalLayout_3.addWidget(self.ParentConnection)


        self.verticalLayout_2.addWidget(self.FrameParent)

        self.FrameStatus = QFrame(SetNode)
        self.FrameStatus.setObjectName(u"FrameStatus")
        self.FrameStatus.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameStatus.setFrameShadow(QFrame.Shadow.Raised)
        self.verticalLayout = QVBoxLayout(self.FrameStatus)
        self.verticalLayout.setObjectName(u"verticalLayout")
        self.verticalLayout.setContentsMargins(5, 3, 5, 3)
        self.Title = QLabel(self.FrameStatus)
        self.Title.setObjectName(u"Title")
        self.Title.setFrameShape(QFrame.Shape.Box)
        self.Title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.verticalLayout.addWidget(self.Title)

        self.Status = QLabel(self.FrameStatus)
        self.Status.setObjectName(u"Status")
        self.Status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.verticalLayout.addWidget(self.Status)


        self.verticalLayout_2.addWidget(self.FrameStatus)

        self.FrameValues = QFrame(SetNode)
        self.FrameValues.setObjectName(u"FrameValues")
        self.FrameValues.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameValues.setFrameShadow(QFrame.Shadow.Raised)
        self.gridLayout = QGridLayout(self.FrameValues)
        self.gridLayout.setObjectName(u"gridLayout")
        self.gridLayout.setHorizontalSpacing(6)
        self.gridLayout.setVerticalSpacing(3)
        self.gridLayout.setContentsMargins(5, 3, 5, 3)
        self.ValueNameLabel = QLabel(self.FrameValues)
        self.ValueNameLabel.setObjectName(u"ValueNameLabel")

        self.gridLayout.addWidget(self.ValueNameLabel, 0, 0, 1, 1)

        self.ValueName = QComboBox(self.FrameValues)
        self.ValueName.setObjectName(u"ValueName")

        self.gridLayout.addWidget(self.ValueName, 0, 1, 1, 1)

        self.CompareToLabel = QLabel(self.FrameValues)
        self.CompareToLabel.setObjectName(u"CompareToLabel")

        self.gridLayout.addWidget(self.CompareToLabel, 1, 0, 1, 1)

        self.CompareTo = QComboBox(self.FrameValues)
        self.CompareTo.setObjectName(u"CompareTo")

        self.gridLayout.addWidget(self.CompareTo, 1, 1, 1, 1)

        self.LiteralValueLabel = QLabel(self.FrameValues)
        self.LiteralValueLabel.setObjectName(u"LiteralValueLabel")

        self.gridLayout.addWidget(self.LiteralValueLabel, 2, 0, 1, 1)

        self.LiteralValue = QLineEdit(self.FrameValues)
        self.LiteralValue.setObjectName(u"LiteralValue")

        self.gridLayout.addWidget(self.LiteralValue, 2, 1, 1, 1)


        self.verticalLayout_2.addWidget(self.FrameValues)


        self.retranslateUi(SetNode)

        QMetaObject.connectSlotsByName(SetNode)
    # setupUi

    def retranslateUi(self, SetNode):
        SetNode.setWindowTitle(QCoreApplication.translate("SetNode", u"Set", None))
        self.ParentConnection.setText(QCoreApplication.translate("SetNode", u"Parent", None))
        self.Title.setText(QCoreApplication.translate("SetNode", u"Title", None))
        self.Status.setText(QCoreApplication.translate("SetNode", u"Status", None))
        self.ValueNameLabel.setText(QCoreApplication.translate("SetNode", u"Value Name", None))
#if QT_CONFIG(tooltip)
        self.ValueName.setToolTip(QCoreApplication.translate("SetNode", u"The blackboard value to set", None))
#endif // QT_CONFIG(tooltip)
        self.CompareToLabel.setText(QCoreApplication.translate("SetNode", u"Set To", None))
#if QT_CONFIG(tooltip)
        self.CompareTo.setToolTip(QCoreApplication.translate("SetNode", u"The Literal Value, or another blackboard value of the same type whose value is copied", None))
#endif // QT_CONFIG(tooltip)
        self.LiteralValueLabel.setText(QCoreApplication.translate("SetNode", u"Literal Value", None))
    # retranslateUi

