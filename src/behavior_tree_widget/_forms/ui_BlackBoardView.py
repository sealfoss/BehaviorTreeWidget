# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'BlackBoardView.ui'
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
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame,
    QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget)

class Ui_BlackBoardEntries(object):
    def setupUi(self, BlackBoardEntries):
        if not BlackBoardEntries.objectName():
            BlackBoardEntries.setObjectName(u"BlackBoardEntries")
        BlackBoardEntries.resize(490, 216)
        self.verticalLayout = QVBoxLayout(BlackBoardEntries)
        self.verticalLayout.setObjectName(u"verticalLayout")
        self.verticalLayout.setContentsMargins(5, 3, 5, 3)
        self.FrameHeader = QFrame(BlackBoardEntries)
        self.FrameHeader.setObjectName(u"FrameHeader")
        self.FrameHeader.setFrameShape(QFrame.Shape.NoFrame)
        self.FrameHeader.setFrameShadow(QFrame.Shadow.Raised)
        self.horizontalLayout_3 = QHBoxLayout(self.FrameHeader)
        self.horizontalLayout_3.setObjectName(u"horizontalLayout_3")
        self.horizontalLayout_3.setContentsMargins(0, 0, 0, 0)
        self.FrameSelection = QFrame(self.FrameHeader)
        self.FrameSelection.setObjectName(u"FrameSelection")
        self.FrameSelection.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameSelection.setFrameShadow(QFrame.Shadow.Raised)
        self.verticalLayout_3 = QVBoxLayout(self.FrameSelection)
        self.verticalLayout_3.setObjectName(u"verticalLayout_3")
        self.verticalLayout_3.setContentsMargins(5, 3, 5, 3)
        self.SelectAll = QCheckBox(self.FrameSelection)
        self.SelectAll.setObjectName(u"SelectAll")

        self.verticalLayout_3.addWidget(self.SelectAll)


        self.horizontalLayout_3.addWidget(self.FrameSelection)

        self.FrameTitles = QFrame(self.FrameHeader)
        self.FrameTitles.setObjectName(u"FrameTitles")
        self.FrameTitles.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameTitles.setFrameShadow(QFrame.Shadow.Raised)
        self.horizontalLayout_5 = QHBoxLayout(self.FrameTitles)
        self.horizontalLayout_5.setObjectName(u"horizontalLayout_5")
        self.horizontalLayout_5.setContentsMargins(5, 3, 5, 3)
        self.LabelEntryName = QLabel(self.FrameTitles)
        self.LabelEntryName.setObjectName(u"LabelEntryName")
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.LabelEntryName.sizePolicy().hasHeightForWidth())
        self.LabelEntryName.setSizePolicy(sizePolicy)
        self.LabelEntryName.setFrameShape(QFrame.Shape.Box)
        self.LabelEntryName.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.horizontalLayout_5.addWidget(self.LabelEntryName)

        self.LabelEntryValue = QLabel(self.FrameTitles)
        self.LabelEntryValue.setObjectName(u"LabelEntryValue")
        sizePolicy.setHeightForWidth(self.LabelEntryValue.sizePolicy().hasHeightForWidth())
        self.LabelEntryValue.setSizePolicy(sizePolicy)
        self.LabelEntryValue.setFrameShape(QFrame.Shape.Box)
        self.LabelEntryValue.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.horizontalLayout_5.addWidget(self.LabelEntryValue)


        self.horizontalLayout_3.addWidget(self.FrameTitles)

        self.FrameResetTitle = QFrame(self.FrameHeader)
        self.FrameResetTitle.setObjectName(u"FrameResetTitle")
        self.FrameResetTitle.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameResetTitle.setFrameShadow(QFrame.Shadow.Raised)
        self.layoutResetTitle = QVBoxLayout(self.FrameResetTitle)
        self.layoutResetTitle.setObjectName(u"layoutResetTitle")
        self.layoutResetTitle.setContentsMargins(5, 3, 5, 3)
        self.LabelEntryReset = QLabel(self.FrameResetTitle)
        self.LabelEntryReset.setObjectName(u"LabelEntryReset")
        self.LabelEntryReset.setFrameShape(QFrame.Shape.Box)
        self.LabelEntryReset.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.layoutResetTitle.addWidget(self.LabelEntryReset)


        self.horizontalLayout_3.addWidget(self.FrameResetTitle)

        self.FrameOrderTitle = QFrame(self.FrameHeader)
        self.FrameOrderTitle.setObjectName(u"FrameOrderTitle")
        self.FrameOrderTitle.setFrameShape(QFrame.Shape.StyledPanel)
        self.FrameOrderTitle.setFrameShadow(QFrame.Shadow.Raised)
        self.layoutOrderTitle = QVBoxLayout(self.FrameOrderTitle)
        self.layoutOrderTitle.setObjectName(u"layoutOrderTitle")
        self.layoutOrderTitle.setContentsMargins(5, 3, 5, 3)
        self.LabelEntryOrder = QLabel(self.FrameOrderTitle)
        self.LabelEntryOrder.setObjectName(u"LabelEntryOrder")
        self.LabelEntryOrder.setFrameShape(QFrame.Shape.Box)
        self.LabelEntryOrder.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.layoutOrderTitle.addWidget(self.LabelEntryOrder)


        self.horizontalLayout_3.addWidget(self.FrameOrderTitle)


        self.verticalLayout.addWidget(self.FrameHeader)

        self.EntryList = QScrollArea(BlackBoardEntries)
        self.EntryList.setObjectName(u"EntryList")
        self.EntryList.setMinimumSize(QSize(480, 46))
        self.EntryList.setWidgetResizable(True)
        self.scrollAreaWidgetContents = QWidget()
        self.scrollAreaWidgetContents.setObjectName(u"scrollAreaWidgetContents")
        self.scrollAreaWidgetContents.setGeometry(QRect(0, 0, 476, 134))
        self.EntryList.setWidget(self.scrollAreaWidgetContents)

        self.verticalLayout.addWidget(self.EntryList)

        self.FrameBlackBoardControls = QFrame(BlackBoardEntries)
        self.FrameBlackBoardControls.setObjectName(u"FrameBlackBoardControls")
        self.FrameBlackBoardControls.setFrameShape(QFrame.Shape.NoFrame)
        self.FrameBlackBoardControls.setFrameShadow(QFrame.Shadow.Raised)
        self.horizontalLayout_4 = QHBoxLayout(self.FrameBlackBoardControls)
        self.horizontalLayout_4.setObjectName(u"horizontalLayout_4")
        self.horizontalLayout_4.setContentsMargins(0, 0, 0, 0)
        self.EntryType = QComboBox(self.FrameBlackBoardControls)
        self.EntryType.addItem("")
        self.EntryType.addItem("")
        self.EntryType.addItem("")
        self.EntryType.addItem("")
        self.EntryType.addItem("")
        self.EntryType.addItem("")
        self.EntryType.setObjectName(u"EntryType")

        self.horizontalLayout_4.addWidget(self.EntryType)

        self.AddEntry = QPushButton(self.FrameBlackBoardControls)
        self.AddEntry.setObjectName(u"AddEntry")

        self.horizontalLayout_4.addWidget(self.AddEntry)

        self.RemoveEntries = QPushButton(self.FrameBlackBoardControls)
        self.RemoveEntries.setObjectName(u"RemoveEntries")

        self.horizontalLayout_4.addWidget(self.RemoveEntries)


        self.verticalLayout.addWidget(self.FrameBlackBoardControls)


        self.retranslateUi(BlackBoardEntries)

        QMetaObject.connectSlotsByName(BlackBoardEntries)
    # setupUi

    def retranslateUi(self, BlackBoardEntries):
        BlackBoardEntries.setWindowTitle(QCoreApplication.translate("BlackBoardEntries", u"Black Board Entries", None))
        self.SelectAll.setText(QCoreApplication.translate("BlackBoardEntries", u"Select All", None))
        self.LabelEntryName.setText(QCoreApplication.translate("BlackBoardEntries", u"Entry Name", None))
        self.LabelEntryValue.setText(QCoreApplication.translate("BlackBoardEntries", u"Entry Value", None))
#if QT_CONFIG(tooltip)
        self.LabelEntryReset.setToolTip(QCoreApplication.translate("BlackBoardEntries", u"Entries with Resets checked go back to their loop-start value at the end of every execution loop", None))
#endif // QT_CONFIG(tooltip)
        self.LabelEntryReset.setText(QCoreApplication.translate("BlackBoardEntries", u"Resets", None))
#if QT_CONFIG(tooltip)
        self.LabelEntryOrder.setToolTip(QCoreApplication.translate("BlackBoardEntries", u"Move an entry up or down in the list with its arrows; the order is saved with the blackboard", None))
#endif // QT_CONFIG(tooltip)
        self.LabelEntryOrder.setText(QCoreApplication.translate("BlackBoardEntries", u"Order", None))
        self.EntryType.setItemText(0, QCoreApplication.translate("BlackBoardEntries", u"Integer", None))
        self.EntryType.setItemText(1, QCoreApplication.translate("BlackBoardEntries", u"Double", None))
        self.EntryType.setItemText(2, QCoreApplication.translate("BlackBoardEntries", u"String", None))
        self.EntryType.setItemText(3, QCoreApplication.translate("BlackBoardEntries", u"List", None))
        self.EntryType.setItemText(4, QCoreApplication.translate("BlackBoardEntries", u"Dictionary", None))
        self.EntryType.setItemText(5, QCoreApplication.translate("BlackBoardEntries", u"Set", None))

        self.AddEntry.setText(QCoreApplication.translate("BlackBoardEntries", u"Add Entry", None))
        self.RemoveEntries.setText(QCoreApplication.translate("BlackBoardEntries", u"Remove Entries", None))
    # retranslateUi

