# !/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@author: Sam Schott  (ss2151@cam.ac.uk)
(c) Sam Schott; This work is licensed under a Creative Commons
Attribution-NonCommercial-NoDerivs 2.0 UK: England & Wales License.
"""

# external packages
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter

#from .dark_mode_support import isDarkWindow


class Spinner(QtWidgets.QWidget):
    """
    A macOS style spinning progress indicator. ``QProgressIndicator`` automatically
    detects and adjusts to "dark mode" appearances.
    """

    m_angle = None
    m_timerId = None
    m_delay = None
    m_displayedWhenStopped = None
    m_color = None
    m_light_color = QtGui.QColor(230, 230, 230)
    m_dark_color = QtGui.QColor(40, 40, 40)

    @property
    def isAnimated(self):
        return self.m_timerId != -1
    @isAnimated.setter
    def isAnimated(self, animated):
        self.startAnimation() if animated else self.stopAnimation()


    def __init__(self, parent=None, color=m_dark_color):
        # Call parent class constructor first
        super(Spinner, self).__init__(parent)

        # Initialize instance variables
        self.m_angle = 0
        self.m_timerId = -1
        self.m_delay = 5/60*1000
        self.m_displayedWhenStopped = False
        self.m_color = color

        self.update_dark_mode()

        # Set size and focus policy
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.startAnimation()

    def animationDelay(self):
        return self.m_delay



    def isDisplayedWhenStopped(self):
        return self.m_displayedWhenStopped

    def getColor(self):
        return self.m_color

    def sizeHint(self):
        return QtCore.QSize(20, 20)

    def startAnimation(self):
        self.m_angle = 0

        if self.m_timerId == -1:
            self.m_timerId = self.startTimer(int(self.m_delay))

    def stopAnimation(self):
        if self.m_timerId != -1:
            self.killTimer(self.m_timerId)

        self.m_timerId = -1
        self.update()

    def setAnimationDelay(self, delay):
        if self.m_timerId != -1:
            self.killTimer(self.m_timerId)

        self.m_delay = delay

        if self.m_timerId != -1:
            self.m_timerId = self.startTimer(self.m_delay)

    def setDisplayedWhenStopped(self, state):
        self.m_displayedWhenStopped = state
        self.update()

    def setColor(self, color):
        self.m_color = color
        self.update()

    def timerEvent(self, event):
        self.m_angle = (self.m_angle + 30) % 360
        self.update()

    def paintEvent(self, event):
        if (not self.m_displayedWhenStopped) and (not self.isAnimated):
            return

        width = min(self.width(), self.height())

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        outerRadius = (width - 1) * 0.5
        innerRadius = (width - 1) * 0.5 * 0.4375

        capsuleHeight = outerRadius - innerRadius
        capsuleWidth  = width * 3/32
        capsuleRadius = capsuleWidth / 2

        for i in range(0, 12):
            color = QtGui.QColor(self.m_color)

            if self.isAnimated:
                color.setAlphaF(1.0 - (i / 12.0))
            else:
                color.setAlphaF(0.2)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.save()
            painter.translate(self.rect().center())
            painter.rotate(self.m_angle - (i * 30.0))
            rect = QRectF(capsuleWidth * -0.5, (innerRadius + capsuleHeight) * -1, capsuleWidth, capsuleHeight)
            painter.drawRoundedRect(rect, capsuleRadius, capsuleRadius)
            painter.restore()

    def changeEvent(self, QEvent):

        if QEvent.type() == QtCore.QEvent.Type.PaletteChange:
            self.update_dark_mode()

    def update_dark_mode(self):
        # if isDarkWindow():
        #     self.setColor(self.m_light_color)
        # else:
            self.setColor(self.m_dark_color)