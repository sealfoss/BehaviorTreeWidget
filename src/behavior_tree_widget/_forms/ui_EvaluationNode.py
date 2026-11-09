# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'EvaluationNode.ui'
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

class Ui_EvaluationNode(object):
    def setupUi(self, EvaluationNode):
        if not EvaluationNode.objectName():
            EvaluationNode.setObjectName(u"EvaluationNode")
        EvaluationNode.resize(260, 190)
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(EvaluationNode.sizePolicy().hasHeightForWidth())
        EvaluationNode.setSizePolicy(sizePolicy)
        self.verticalLayout_2 = QVBoxLayout(EvaluationNode)
        self.verticalLayout_2.setObjectName(u"verticalLayout_2")
        self.verticalLayout_2.setContentsMargins(5, 3, 5, 3)
        self.FrameParent = QFrame(EvaluationNode)
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

        self.FrameStatus = QFrame(EvaluationNode)
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

        self.FrameValues = QFrame(EvaluationNode)
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


        self.retranslateUi(EvaluationNode)

        QMetaObject.connectSlotsByName(EvaluationNode)
    # setupUi

    def retranslateUi(self, EvaluationNode):
        EvaluationNode.setWindowTitle(QCoreApplication.translate("EvaluationNode", u"Evaluation", None))
        self.ParentConnection.setText(QCoreApplication.translate("EvaluationNode", u"Parent", None))
        self.Title.setText(QCoreApplication.translate("EvaluationNode", u"Title", None))
        self.Status.setText(QCoreApplication.translate("EvaluationNode", u"Status", None))
        self.ValueNameLabel.setText(QCoreApplication.translate("EvaluationNode", u"Value Name", None))
#if QT_CONFIG(tooltip)
        self.ValueName.setToolTip(QCoreApplication.translate("EvaluationNode", u"The blackboard value to evaluate", None))
#endif // QT_CONFIG(tooltip)
        self.CompareToLabel.setText(QCoreApplication.translate("EvaluationNode", u"Compare To", None))
#if QT_CONFIG(tooltip)
        self.CompareTo.setToolTip(QCoreApplication.translate("EvaluationNode", u"The Literal Value, or another blackboard value of the same type", None))
#endif // QT_CONFIG(tooltip)
        self.LiteralValueLabel.setText(QCoreApplication.translate("EvaluationNode", u"Literal Value", None))
    # retranslateUi

