# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'GroupNode.ui'
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
    QLineEdit, QSizePolicy, QSpacerItem, QVBoxLayout,
    QWidget)

class Ui_GroupNode(object):
    def setupUi(self, GroupNode):
        if not GroupNode.objectName():
            GroupNode.setObjectName(u"GroupNode")
        GroupNode.resize(360, 240)
        self.layoutGroup = QVBoxLayout(GroupNode)
        self.layoutGroup.setSpacing(0)
        self.layoutGroup.setObjectName(u"layoutGroup")
        self.layoutGroup.setContentsMargins(0, 0, 0, 0)
        self.GroupHeader = QFrame(GroupNode)
        self.GroupHeader.setObjectName(u"GroupHeader")
        self.GroupHeader.setFrameShape(QFrame.Shape.NoFrame)
        self.layoutGroupHeader = QHBoxLayout(self.GroupHeader)
        self.layoutGroupHeader.setSpacing(4)
        self.layoutGroupHeader.setObjectName(u"layoutGroupHeader")
        self.layoutGroupHeader.setContentsMargins(8, 6, 8, 4)
        self.GroupName = QLineEdit(self.GroupHeader)
        self.GroupName.setObjectName(u"GroupName")

        self.layoutGroupHeader.addWidget(self.GroupName)

        self.spacerGroupHeader = QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.layoutGroupHeader.addItem(self.spacerGroupHeader)


        self.layoutGroup.addWidget(self.GroupHeader)

        self.Title = QLabel(GroupNode)
        self.Title.setObjectName(u"Title")

        self.layoutGroup.addWidget(self.Title)

        self.Status = QLabel(GroupNode)
        self.Status.setObjectName(u"Status")

        self.layoutGroup.addWidget(self.Status)

        self.spacerGroupInterior = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)

        self.layoutGroup.addItem(self.spacerGroupInterior)


        self.retranslateUi(GroupNode)

        QMetaObject.connectSlotsByName(GroupNode)
    # setupUi

    def retranslateUi(self, GroupNode):
        GroupNode.setWindowTitle(QCoreApplication.translate("GroupNode", u"Group", None))
#if QT_CONFIG(tooltip)
        self.GroupName.setToolTip(QCoreApplication.translate("GroupNode", u"Group name", None))
#endif // QT_CONFIG(tooltip)
        self.GroupName.setPlaceholderText(QCoreApplication.translate("GroupNode", u"Group name", None))
        self.Title.setText(QCoreApplication.translate("GroupNode", u"Group", None))
        self.Status.setText(QCoreApplication.translate("GroupNode", u"Status", None))
    # retranslateUi

