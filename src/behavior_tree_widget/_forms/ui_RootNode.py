# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'RootNode.ui'
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
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QSizePolicy,
    QVBoxLayout, QWidget)

class Ui_RootNode(object):
    def setupUi(self, RootNode):
        if not RootNode.objectName():
            RootNode.setObjectName(u"RootNode")
        RootNode.resize(168, 88)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(RootNode.sizePolicy().hasHeightForWidth())
        RootNode.setSizePolicy(sizePolicy)
        self.verticalLayout_2 = QVBoxLayout(RootNode)
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.verticalLayout_2.setContentsMargins(5, 3, 5, 3)
        self.FrameStatus = QFrame(RootNode)
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

        self.FrameChildren = QFrame(RootNode)
        self.FrameChildren.setObjectName(u"FrameChildren")
        self.FrameChildren.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameChildren.setFrameShadow(QFrame.Shadow.Raised)
        self.verticalLayout_4 = QVBoxLayout(self.FrameChildren)
        self.verticalLayout_4.setObjectName(u"verticalLayout_4")
        self.verticalLayout_4.setContentsMargins(5, 3, 5, 3)
        self.ChildConnections = QLabel(self.FrameChildren)
        self.ChildConnections.setObjectName(u"ChildConnections")
        self.ChildConnections.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.verticalLayout_4.addWidget(self.ChildConnections)


        self.verticalLayout_2.addWidget(self.FrameChildren)


        self.retranslateUi(RootNode)

        QMetaObject.connectSlotsByName(RootNode)
    # setupUi

    def retranslateUi(self, RootNode):
        RootNode.setWindowTitle(QCoreApplication.translate("RootNode", u"Root", None))
        self.Title.setText(QCoreApplication.translate("RootNode", u"Title", None))
        self.Status.setText(QCoreApplication.translate("RootNode", u"Status", None))
        self.ChildConnections.setText(QCoreApplication.translate("RootNode", u"Children", None))
    # retranslateUi

