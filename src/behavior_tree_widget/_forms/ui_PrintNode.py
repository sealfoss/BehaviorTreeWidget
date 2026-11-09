# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'PrintNode.ui'
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

class Ui_PrintNode(object):
    def setupUi(self, PrintNode):
        if not PrintNode.objectName():
            PrintNode.setObjectName(u"PrintNode")
        PrintNode.resize(260, 190)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(PrintNode.sizePolicy().hasHeightForWidth())
        PrintNode.setSizePolicy(sizePolicy)
        self.verticalLayout_2 = QVBoxLayout(PrintNode)
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.verticalLayout_2.setContentsMargins(5, 3, 5, 3)
        self.FrameParent = QFrame(PrintNode)
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

        self.FrameStatus = QFrame(PrintNode)
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

        self.FrameValues = QFrame(PrintNode)
        self.FrameValues.setObjectName(u"FrameValues")
        self.FrameValues.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameValues.setFrameShadow(QFrame.Shadow.Raised)
        self.gridLayout = QGridLayout(self.FrameValues)
        self.gridLayout.setObjectName(u"gridLayout")
        self.gridLayout.setHorizontalSpacing(6)
        self.gridLayout.setVerticalSpacing(3)
        self.gridLayout.setContentsMargins(5, 3, 5, 3)
        self.LogLevelLabel = QLabel(self.FrameValues)
        self.LogLevelLabel.setObjectName(u"LogLevelLabel")

        self.gridLayout.addWidget(self.LogLevelLabel, 0, 0, 1, 1)

        self.LogLevel = QComboBox(self.FrameValues)
        self.LogLevel.setObjectName(u"LogLevel")

        self.gridLayout.addWidget(self.LogLevel, 0, 1, 1, 1)

        self.SourceLabel = QLabel(self.FrameValues)
        self.SourceLabel.setObjectName(u"SourceLabel")

        self.gridLayout.addWidget(self.SourceLabel, 1, 0, 1, 1)

        self.Source = QComboBox(self.FrameValues)
        self.Source.setObjectName(u"Source")

        self.gridLayout.addWidget(self.Source, 1, 1, 1, 1)

        self.MessageLabel = QLabel(self.FrameValues)
        self.MessageLabel.setObjectName(u"MessageLabel")

        self.gridLayout.addWidget(self.MessageLabel, 2, 0, 1, 1)

        self.Message = QLineEdit(self.FrameValues)
        self.Message.setObjectName(u"Message")

        self.gridLayout.addWidget(self.Message, 2, 1, 1, 1)


        self.verticalLayout_2.addWidget(self.FrameValues)


        self.retranslateUi(PrintNode)

        QMetaObject.connectSlotsByName(PrintNode)
    # setupUi

    def retranslateUi(self, PrintNode):
        PrintNode.setWindowTitle(QCoreApplication.translate("PrintNode", u"Print Message", None))
        self.ParentConnection.setText(QCoreApplication.translate("PrintNode", u"Parent", None))
        self.Title.setText(QCoreApplication.translate("PrintNode", u"Title", None))
        self.Status.setText(QCoreApplication.translate("PrintNode", u"Status", None))
        self.LogLevelLabel.setText(QCoreApplication.translate("PrintNode", u"Level", None))
#if QT_CONFIG(tooltip)
        self.LogLevel.setToolTip(QCoreApplication.translate("PrintNode", u"The log level the message is written with (0: None prints nothing)", None))
#endif // QT_CONFIG(tooltip)
        self.SourceLabel.setText(QCoreApplication.translate("PrintNode", u"Print", None))
#if QT_CONFIG(tooltip)
        self.Source.setToolTip(QCoreApplication.translate("PrintNode", u"Literal prints the Message; a blackboard entry prints its name and value", None))
#endif // QT_CONFIG(tooltip)
        self.MessageLabel.setText(QCoreApplication.translate("PrintNode", u"Message", None))
#if QT_CONFIG(tooltip)
        self.Message.setToolTip(QCoreApplication.translate("PrintNode", u"The text written to the log", None))
#endif // QT_CONFIG(tooltip)
        self.Message.setPlaceholderText(QCoreApplication.translate("PrintNode", u"Text to print", None))
    # retranslateUi

