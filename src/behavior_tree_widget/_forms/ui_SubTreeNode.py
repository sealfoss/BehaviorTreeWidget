# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'SubTreeNode.ui'
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
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QSizePolicy, QToolButton, QVBoxLayout,
    QWidget)

class Ui_SubTreeNode(object):
    def setupUi(self, SubTreeNode):
        if not SubTreeNode.objectName():
            SubTreeNode.setObjectName(u"SubTreeNode")
        SubTreeNode.resize(288, 308)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(SubTreeNode.sizePolicy().hasHeightForWidth())
        SubTreeNode.setSizePolicy(sizePolicy)
        self.verticalLayout_2 = QVBoxLayout(SubTreeNode)
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.verticalLayout_2.setContentsMargins(5, 3, 5, 3)
        self.FrameParent = QFrame(SubTreeNode)
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

        self.FrameStatus = QFrame(SubTreeNode)
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

        self.FrameSubTree = QFrame(SubTreeNode)
        self.FrameSubTree.setObjectName(u"FrameSubTree")
        self.FrameSubTree.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameSubTree.setFrameShadow(QFrame.Shadow.Raised)
        self.layoutSubTree = QHBoxLayout(self.FrameSubTree)
        self.layoutSubTree.setSpacing(3)
        self.layoutSubTree.setObjectName(u"layoutSubTree")
        self.layoutSubTree.setContentsMargins(4, 3, 4, 3)
        self.SubTreeFileLabel = QLabel(self.FrameSubTree)
        self.SubTreeFileLabel.setObjectName(u"SubTreeFileLabel")

        self.layoutSubTree.addWidget(self.SubTreeFileLabel)

        self.SubTreeFile = QLineEdit(self.FrameSubTree)
        self.SubTreeFile.setObjectName(u"SubTreeFile")
        self.SubTreeFile.setMinimumSize(QSize(150, 0))

        self.layoutSubTree.addWidget(self.SubTreeFile)

        self.BrowseSubTree = QToolButton(self.FrameSubTree)
        self.BrowseSubTree.setObjectName(u"BrowseSubTree")

        self.layoutSubTree.addWidget(self.BrowseSubTree)


        self.verticalLayout_2.addWidget(self.FrameSubTree)


        self.retranslateUi(SubTreeNode)

        QMetaObject.connectSlotsByName(SubTreeNode)
    # setupUi

    def retranslateUi(self, SubTreeNode):
        SubTreeNode.setWindowTitle(QCoreApplication.translate("SubTreeNode", u"Sub-Tree", None))
        self.ParentConnection.setText(QCoreApplication.translate("SubTreeNode", u"Parent", None))
        self.Title.setText(QCoreApplication.translate("SubTreeNode", u"Title", None))
        self.Status.setText(QCoreApplication.translate("SubTreeNode", u"Status", None))
        self.SubTreeFileLabel.setText(QCoreApplication.translate("SubTreeNode", u"File", None))
        self.SubTreeFile.setPlaceholderText(QCoreApplication.translate("SubTreeNode", u"tree_BT.json", None))
#if QT_CONFIG(tooltip)
        self.BrowseSubTree.setToolTip(QCoreApplication.translate("SubTreeNode", u"Choose the behavior tree file to run", None))
#endif // QT_CONFIG(tooltip)
        self.BrowseSubTree.setText(QCoreApplication.translate("SubTreeNode", u"...", None))
    # retranslateUi

