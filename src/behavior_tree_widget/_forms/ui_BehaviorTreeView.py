# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'BehaviorTreeView.ui'
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
from PySide6.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget)

class Ui_BehaviorTreeView(object):
    def setupUi(self, BehaviorTreeView):
        if not BehaviorTreeView.objectName():
            BehaviorTreeView.setObjectName(u"BehaviorTreeView")
        BehaviorTreeView.resize(1165, 894)
        self.verticalLayout = QVBoxLayout(BehaviorTreeView)
        self.verticalLayout.setObjectName(u"verticalLayout")
        self.verticalLayout.setContentsMargins(5, 3, 5, 3)
        self.FrameView = QFrame(BehaviorTreeView)
        self.FrameView.setObjectName(u"FrameView")
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.FrameView.sizePolicy().hasHeightForWidth())
        self.FrameView.setSizePolicy(sizePolicy)
        self.FrameView.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameView.setFrameShadow(QFrame.Shadow.Raised)

        self.verticalLayout.addWidget(self.FrameView)

        self.FrameExecution = QFrame(BehaviorTreeView)
        self.FrameExecution.setObjectName(u"FrameExecution")
        sizePolicy1 = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sizePolicy1.setHorizontalStretch(0)
        sizePolicy1.setVerticalStretch(0)
        sizePolicy1.setHeightForWidth(self.FrameExecution.sizePolicy().hasHeightForWidth())
        self.FrameExecution.setSizePolicy(sizePolicy1)
        self.FrameExecution.setMaximumSize(QSize(16777215, 41))
        self.FrameExecution.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameExecution.setFrameShadow(QFrame.Shadow.Raised)
        self.horizontalLayout_2 = QHBoxLayout(self.FrameExecution)
        self.horizontalLayout_2.setObjectName(u"horizontalLayout_2")
        self.horizontalLayout_2.setContentsMargins(5, 3, 5, 3)
        self.Execute = QPushButton(self.FrameExecution)
        self.Execute.setObjectName(u"Execute")
        self.Execute.setMinimumSize(QSize(41, 26))
        self.Execute.setMaximumSize(QSize(41, 26))
        font = QFont()
        font.setFamilies([u"Segoe UI"])
        font.setPointSize(11)
        font.setBold(True)
        self.Execute.setFont(font)

        self.horizontalLayout_2.addWidget(self.Execute)

        self.Pause = QPushButton(self.FrameExecution)
        self.Pause.setObjectName(u"Pause")
        self.Pause.setMinimumSize(QSize(41, 26))
        self.Pause.setMaximumSize(QSize(41, 26))
        font1 = QFont()
        font1.setFamilies([u"Segoe UI"])
        font1.setPointSize(11)
        font1.setBold(True)
        font1.setKerning(False)
        self.Pause.setFont(font1)

        self.horizontalLayout_2.addWidget(self.Pause)

        self.Stop = QPushButton(self.FrameExecution)
        self.Stop.setObjectName(u"Stop")
        self.Stop.setMinimumSize(QSize(41, 26))
        self.Stop.setMaximumSize(QSize(41, 26))
        font2 = QFont()
        font2.setPointSize(11)
        font2.setBold(True)
        self.Stop.setFont(font2)

        self.horizontalLayout_2.addWidget(self.Stop)

        self.Reset = QPushButton(self.FrameExecution)
        self.Reset.setObjectName(u"Reset")
        self.Reset.setMinimumSize(QSize(41, 26))
        self.Reset.setMaximumSize(QSize(41, 26))
        self.Reset.setFont(font2)

        self.horizontalLayout_2.addWidget(self.Reset)

        self.LoopExecution = QCheckBox(self.FrameExecution)
        self.LoopExecution.setObjectName(u"LoopExecution")
        sizePolicy2 = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy2.setHorizontalStretch(0)
        sizePolicy2.setVerticalStretch(0)
        sizePolicy2.setHeightForWidth(self.LoopExecution.sizePolicy().hasHeightForWidth())
        self.LoopExecution.setSizePolicy(sizePolicy2)
        self.LoopExecution.setChecked(False)

        self.horizontalLayout_2.addWidget(self.LoopExecution)

        self.Configure = QPushButton(self.FrameExecution)
        self.Configure.setObjectName(u"Configure")
        self.Configure.setMinimumSize(QSize(41, 26))
        self.Configure.setMaximumSize(QSize(41, 26))
        font3 = QFont()
        font3.setFamilies([u"Arial"])
        font3.setPointSize(11)
        self.Configure.setFont(font3)

        self.horizontalLayout_2.addWidget(self.Configure)

        self.Save = QPushButton(self.FrameExecution)
        self.Save.setObjectName(u"Save")
        self.Save.setMaximumSize(QSize(61, 16777215))

        self.horizontalLayout_2.addWidget(self.Save)

        self.Load = QPushButton(self.FrameExecution)
        self.Load.setObjectName(u"Load")
        self.Load.setMaximumSize(QSize(61, 16777215))

        self.horizontalLayout_2.addWidget(self.Load)

        self.New = QPushButton(self.FrameExecution)
        self.New.setObjectName(u"New")
        self.New.setMaximumSize(QSize(61, 16777215))

        self.horizontalLayout_2.addWidget(self.New)

        self.GoBack = QPushButton(self.FrameExecution)
        self.GoBack.setObjectName(u"GoBack")
        self.GoBack.setEnabled(False)
        self.GoBack.setMaximumSize(QSize(71, 26))

        self.horizontalLayout_2.addWidget(self.GoBack)


        self.verticalLayout.addWidget(self.FrameExecution)


        self.retranslateUi(BehaviorTreeView)

        QMetaObject.connectSlotsByName(BehaviorTreeView)
    # setupUi

    def retranslateUi(self, BehaviorTreeView):
        BehaviorTreeView.setWindowTitle(QCoreApplication.translate("BehaviorTreeView", u"Behavior Tree", None))
#if QT_CONFIG(tooltip)
        self.Execute.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Execute the tree (resume when paused)", None))
#endif // QT_CONFIG(tooltip)
        self.Execute.setText(QCoreApplication.translate("BehaviorTreeView", u"\u25b6\ufe0f", None))
#if QT_CONFIG(tooltip)
        self.Pause.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Pause execution", None))
#endif // QT_CONFIG(tooltip)
        self.Pause.setText(QCoreApplication.translate("BehaviorTreeView", u"\u23f8", None))
#if QT_CONFIG(tooltip)
        self.Stop.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Stop execution and reset the tree", None))
#endif // QT_CONFIG(tooltip)
        self.Stop.setText(QCoreApplication.translate("BehaviorTreeView", u"\u23f9", None))
#if QT_CONFIG(tooltip)
        self.Reset.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Reset the tree without stopping execution", None))
#endif // QT_CONFIG(tooltip)
        self.Reset.setText(QCoreApplication.translate("BehaviorTreeView", u"\U0001f504", None))
#if QT_CONFIG(tooltip)
        self.LoopExecution.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Execute the tree again each time the root node returns (blackboard and node values are kept)", None))
#endif // QT_CONFIG(tooltip)
        self.LoopExecution.setText(QCoreApplication.translate("BehaviorTreeView", u"Loop Execution", None))
#if QT_CONFIG(tooltip)
        self.Configure.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Configure tree options", None))
#endif // QT_CONFIG(tooltip)
        self.Configure.setText(QCoreApplication.translate("BehaviorTreeView", u"\u2699", None))
#if QT_CONFIG(tooltip)
        self.Save.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Save the tree to a .json file", None))
#endif // QT_CONFIG(tooltip)
        self.Save.setText(QCoreApplication.translate("BehaviorTreeView", u"Save", None))
#if QT_CONFIG(tooltip)
        self.Load.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Load a tree from a .json file", None))
#endif // QT_CONFIG(tooltip)
        self.Load.setText(QCoreApplication.translate("BehaviorTreeView", u"Load", None))
#if QT_CONFIG(tooltip)
        self.New.setToolTip(QCoreApplication.translate("BehaviorTreeView", u"Create a new tree stored in a .json file", None))
#endif // QT_CONFIG(tooltip)
        self.New.setText(QCoreApplication.translate("BehaviorTreeView", u"New", None))
        self.GoBack.setText(QCoreApplication.translate("BehaviorTreeView", u"Go Back", None))
    # retranslateUi

