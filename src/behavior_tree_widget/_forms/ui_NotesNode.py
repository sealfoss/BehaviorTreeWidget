# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'NotesNode.ui'
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

class Ui_NotesNode(object):
    def setupUi(self, NotesNode):
        if not NotesNode.objectName():
            NotesNode.setObjectName(u"NotesNode")
        NotesNode.resize(220, 120)
        NotesNode.setStyleSheet(u"QFrame#NotesNode { background-color: #fff4b8; border: 1px solid #a68b2c; }")
        self.layoutNotes = QVBoxLayout(NotesNode)
        self.layoutNotes.setSpacing(0)
        self.layoutNotes.setObjectName(u"layoutNotes")
        self.layoutNotes.setContentsMargins(0, 0, 0, 0)
        self.Title = QLabel(NotesNode)
        self.Title.setObjectName(u"Title")

        self.layoutNotes.addWidget(self.Title)

        self.Status = QLabel(NotesNode)
        self.Status.setObjectName(u"Status")

        self.layoutNotes.addWidget(self.Status)


        self.retranslateUi(NotesNode)

        QMetaObject.connectSlotsByName(NotesNode)
    # setupUi

    def retranslateUi(self, NotesNode):
        NotesNode.setWindowTitle(QCoreApplication.translate("NotesNode", u"Notes", None))
        self.Title.setText(QCoreApplication.translate("NotesNode", u"Notes", None))
        self.Status.setText(QCoreApplication.translate("NotesNode", u"Status", None))
    # retranslateUi

