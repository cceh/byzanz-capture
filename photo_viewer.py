import math

from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import QResizeEvent


class PhotoViewer(QtWidgets.QGraphicsView):
    photoClicked = QtCore.pyqtSignal(QtCore.QPoint)

    def __init__(self, parent):
        super(PhotoViewer, self).__init__(parent)
        self.parent = parent
        self.isMouseOver = False
        self.ZOOMFACT = 1.25
        self._zoom = 0
        self._zoomfactor = 1
        self._empty = True
        self._scene = QtWidgets.QGraphicsScene(self)
        self._photo = QtWidgets.QGraphicsPixmapItem()
        self._scene.addItem(self._photo)
        self.setScene(self._scene)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setBackgroundBrush(QtGui.QBrush(QtGui.QColor(30, 30, 30)))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

    def hasPhoto(self):
        return not self._empty

    def printUnityFactor(self):
        rect = QtCore.QRectF(self._photo.pixmap().rect())
        unity = self.transform().mapRect(QtCore.QRectF(0, 0, 1, 1))
        viewrect = self.viewport().rect()
        scenerect = self.transform().mapRect(rect)
        factor = min(viewrect.width() / scenerect.width(),
                     viewrect.height() / scenerect.height())
        print("puf factor {} vr_w {} sr_w {} u_w {} vr_h {} sr_h {} u_h {} ".format(factor, viewrect.width(), scenerect.width(), unity.width(), viewrect.height(), scenerect.height(), unity.height() ))

    def fitInView(self, scale=True):
        rect = QtCore.QRectF(self._photo.pixmap().rect())
        if not rect.isNull():
            self.setSceneRect(rect)
            if self.hasPhoto():
                unity = self.transform().mapRect(QtCore.QRectF(0, 0, 1, 1))
                self.scale(1 / unity.width(), 1 / unity.height())
                viewrect = self.viewport().rect()
                scenerect = self.transform().mapRect(rect)
                factor = min(viewrect.width() / scenerect.width(),
                             viewrect.height() / scenerect.height())
                # here, view scaled to fit:
                self._zoomfactor = factor
                self._zoom = math.log( self._zoomfactor, self.ZOOMFACT )
                self.scale(factor, factor)
                # self.parent.updateStatusBar()
                if (self.isMouseOver): # should be true on wheel, regardless
                    self.setDragState()

    def setPhoto(self, pixmap=None):
        if pixmap and not pixmap.isNull():
            self._empty = False
            self._photo.setPixmap(pixmap)
        else:
            self._empty = True
            self._photo.setPixmap(QtGui.QPixmap())

    def resetZoom(self):
        if self.hasPhoto():
            self._zoom = 0
            self._zoomfactor = 1.0
            unity = self.transform().mapRect(QtCore.QRectF(0, 0, 1, 1))
            self.scale(1 / unity.width(), 1 / unity.height())
            # self.parent.updateStatusBar()
            if (self.isMouseOver):
                self.setDragState()

    def zoomPlus(self):
        if self.hasPhoto():
            if self._zoomfactor >= 1.0:
                return
            factor = self.ZOOMFACT # 1.25
            self._zoomfactor = self._zoomfactor * self.ZOOMFACT
            self._zoom += 1
            print(self._zoom)
            self.scale(factor, factor)
            # self.parent.updateStatusBar()
            self.setDragState()

    def zoomMinus(self):
        if self.hasPhoto():
            rect = QtCore.QRectF(self._photo.pixmap().rect())
            # self.scale(1 / unity.width(), 1 / unity.height())
            viewrect = self.viewport().rect()
            scenerect = self.transform().mapRect(rect)
            max_factor = min(viewrect.width() / scenerect.width(),
                         viewrect.height() / scenerect.height())
            print(max_factor)
            factor = 1.0/self.ZOOMFACT #0.8
            print(factor)

            if factor <= max_factor * 0.8:
                return

            self._zoomfactor = self._zoomfactor / self.ZOOMFACT
            print(self._zoomfactor)
            self._zoom -= 1
            self.scale(factor, factor)
            # self.parent.updateStatusBar()
            self.setDragState()

    def wheelEvent(self, event):
        if self.hasPhoto():
            if event.angleDelta().y() > 0:
                self.zoomPlus()
            else:
                self.zoomMinus()

    def mousePressEvent(self, event):
        if self._photo.isUnderMouse():
            self.photoClicked.emit(QtCore.QPoint(event.pos()))
        super(PhotoViewer, self).mousePressEvent(event)

    def getCanDrag(self):
        return ((self.horizontalScrollBar().maximum() > 0) or (self.verticalScrollBar().maximum() > 0))

    def setDragState(self):
        # here we mostly want to take case of the mouse cursor/pointer - and show the hand only when dragging is possible
        canDrag = self.getCanDrag()
        if (canDrag):
            self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        else:
            self.setDragMode(QtWidgets.QGraphicsView.DragMode.NoDrag)

    def enterEvent(self, event):
        self.isMouseOver = True
        self.setDragState()
        return super(PhotoViewer, self).enterEvent(event)

    def leaveEvent(self, event):
        self.isMouseOver = False
        # no need for setDragState - is autohandled, as we leave
        return super(PhotoViewer, self).leaveEvent(event)