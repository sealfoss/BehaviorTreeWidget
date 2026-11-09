# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'LeafNode.ui'
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
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QListView,
    QSizePolicy, QVBoxLayout, QWidget)

class Ui_LeafNode(object):
    def setupUi(self, LeafNode):
        if not LeafNode.objectName():
            LeafNode.setObjectName(u"LeafNode")
        LeafNode.resize(288, 308)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(LeafNode.sizePolicy().hasHeightForWidth())
        LeafNode.setSizePolicy(sizePolicy)
        self.verticalLayout_2 = QVBoxLayout(LeafNode)
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.verticalLayout_2.setContentsMargins(5, 3, 5, 3)
        self.FrameParent = QFrame(LeafNode)
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

        self.FrameStatus = QFrame(LeafNode)
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

        self.FrameFields = QFrame(LeafNode)
        self.FrameFields.setObjectName(u"FrameFields")
        self.FrameFields.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameFields.setFrameShadow(QFrame.Shadow.Raised)
        self.verticalLayout_4 = QVBoxLayout(self.FrameFields)
        self.verticalLayout_4.setObjectName(u"verticalLayout_4")
        self.verticalLayout_4.setContentsMargins(0, 0, 0, 0)
        self.Fields = QListView(self.FrameFields)
        self.Fields.setObjectName(u"Fields")

        self.verticalLayout_4.addWidget(self.Fields)


        self.verticalLayout_2.addWidget(self.FrameFields)


        self.retranslateUi(LeafNode)

        QMetaObject.connectSlotsByName(LeafNode)
    # setupUi

    def retranslateUi(self, LeafNode):
        LeafNode.setWindowTitle(QCoreApplication.translate("LeafNode", u"Leaf", None))
        self.ParentConnection.setText(QCoreApplication.translate("LeafNode", u"Parent", None))
        self.Title.setText(QCoreApplication.translate("LeafNode", u"Title", None))
        self.Status.setText(QCoreApplication.translate("LeafNode", u"Status", None))
    # retranslateUi

