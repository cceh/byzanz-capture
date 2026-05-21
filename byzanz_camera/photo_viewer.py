import math

from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import QEasingCurve, QEvent, Qt, QVariantAnimation
from PyQt6.QtGui import QPixmap


class PhotoViewer(QtWidgets.QGraphicsView):
    photoClicked = QtCore.pyqtSignal(QtCore.QPoint)
    # Emitted whenever the absolute scale changes (fit, step, set,
    # photo swap). Carries the current scale as a float (1.0 = 1:1).
    # Drives the zoom control bar's display.
    zoom_changed = QtCore.pyqtSignal(float)

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
        self._mirror_view: QtWidgets.QGraphicsView | None = None
        self.setScene(self._scene)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setBackgroundBrush(QtGui.QBrush(QtGui.QColor(30, 30, 30)))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        # Forward NativeGesture events that land on either the
        # viewport (the inner QWidget that hosts the scene paint) or
        # the always-on scrollbars back through our own event(). Qt
        # dispatches gestures to the innermost widget under the
        # cursor, so without these filters fast pinches can land on
        # the viewport instead of the QGraphicsView and get dropped.
        self.viewport().installEventFilter(self)
        self.horizontalScrollBar().installEventFilter(self)
        self.verticalScrollBar().installEventFilter(self)
        # Reactive cursor / dragMode — whenever the scrollbar's range
        # changes (after any scale or layout pass), re-evaluate. This
        # replaces a fleet of imperative `self.setDragState()` calls in
        # zoom paths that ran BEFORE Qt had updated scrollbar geometry
        # and thus read stale `maximum()` — leaving the cursor stuck on
        # arrow even when there was room to pan.
        self.horizontalScrollBar().rangeChanged.connect(
            lambda *_: self.setDragState()
        )
        self.verticalScrollBar().rangeChanged.connect(
            lambda *_: self.setDragState()
        )

    def getScene(self):
        return self._scene

    def setMirrorView(self, view: QtWidgets.QGraphicsView):
        self._mirror_view = view
        if self._mirror_view:
            self._mirror_view.setScene(self._scene)
            if self.hasPhoto():
                self.fitMirrorView()

    def fitMirrorView(self):
        photo_rect = QtCore.QRectF(self._photo.pixmap().rect())
        self._mirror_view.setSceneRect(photo_rect)
        self._mirror_view.fitInView(photo_rect, Qt.AspectRatioMode.KeepAspectRatio)

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
                # dragMode now driven reactively by scrollbar.rangeChanged
                # (wired in __init__) — no imperative setDragState here.
                self.zoom_changed.emit(self._zoomfactor)

    # ---- introspection / external control (for the zoom control bar) ----

    def current_scale(self) -> float:
        """Absolute display scale, where 1.0 == 100% (1:1)."""
        return self.transform().m11()

    def fit_scale(self) -> float:
        """Scale that would fit the current photo to the viewport.
        Returns 1.0 when there's no photo (degenerate but harmless)."""
        if not self.hasPhoto():
            return 1.0
        rect = self._photo.pixmap().rect()
        if rect.isEmpty():
            return 1.0
        viewrect = self.viewport().rect()
        return min(viewrect.width() / rect.width(),
                   viewrect.height() / rect.height())

    def _apply_scale(self, target: float) -> bool:
        """Single source of truth for scale changes. Clamps `target`
        to the legal envelope ([fit_scale, max(1.0, fit_scale)]),
        applies the transform, syncs internal state, emits
        `zoom_changed`. Returns True iff the scale actually changed.

        All zoom entry points (slider, ±buttons, wheel, pinch, animation
        steps) funnel through here so clamping + state sync + emit can't
        drift between paths. Cursor / drag-mode is now driven by the
        scrollbar's `rangeChanged` signal (wired in __init__), so we
        don't call setDragState here — it'd race the layout pass."""
        if not self.hasPhoto():
            return False
        lo = self.fit_scale()
        target = max(lo, min(max(1.0, lo), target))
        current = self.current_scale()
        if current <= 0 or abs(target - current) < 1e-6:
            return False
        self.scale(target / current, target / current)
        self._zoomfactor = target
        self._zoom = math.log(target, self.ZOOMFACT)
        self.zoom_changed.emit(target)
        return True

    def set_absolute_scale(self, scale: float) -> None:
        """Apply an absolute scale (clamped to [fit, 1.0]). Used by
        the zoom-bar slider and by 1:1 / Fit buttons."""
        self._apply_scale(scale)

    # ---- animated jumps ------------------------------------------------
    # Reserved for the discrete-jump zoom actions (double-click toggle,
    # Fit/1:1 buttons) where seeing the transition helps orientation.
    # Wheel and slider drag stay instant — animating those would feel
    # laggy because the user is actively driving the value.

    def _animate_to(self, target_scale: float, target_center_scene,
                    duration_ms: int = 180) -> None:
        if not self.hasPhoto():
            return
        start_scale = self.current_scale()
        if start_scale <= 0 or target_scale <= 0:
            return
        # Clamp target to the same [fit, max(1.0, fit)] envelope the
        # other zoom paths (set_absolute_scale, _handle_pinch_zoom)
        # honor. Defensive — callers already pre-clamp today, but
        # pushing it here means future callers can't push the view
        # outside the legal range via animation.
        lo = self.fit_scale()
        hi = max(1.0, lo)
        target_scale = max(lo, min(hi, target_scale))
        start_center = self.mapToScene(self.viewport().rect().center())

        # Cancel any in-flight zoom animation so two interpolations
        # can't fight over the transform.
        prev = getattr(self, "_zoom_anim", None)
        if prev is not None and prev.state() == QVariantAnimation.State.Running:
            prev.stop()

        log_start = math.log(start_scale)
        log_end = math.log(target_scale)
        dx = target_center_scene.x() - start_center.x()
        dy = target_center_scene.y() - start_center.y()

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration_ms)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def step(t: float) -> None:
            # Log-scale interpolation — perceptually constant rate.
            # Linear interpolation visibly ramps at the small end.
            scale = math.exp(log_start + (log_end - log_start) * t)
            self._apply_scale(scale)
            self.centerOn(start_center.x() + dx * t,
                          start_center.y() + dy * t)

        anim.valueChanged.connect(step)
        anim.start()
        # Keep a reference — QVariantAnimation is GC'd otherwise and
        # stops mid-flight.
        self._zoom_anim = anim

    def animated_fit_in_view(self) -> None:
        """Animated zoom-to-fit. Used by the Fit button — the
        instant fitInView() path is kept for initial-load and
        live-frame fits where animating would just look glitchy."""
        if not self.hasPhoto():
            return
        rect = QtCore.QRectF(self._photo.pixmap().rect())
        if rect.isEmpty():
            return
        self.setSceneRect(rect)
        self._animate_to(self.fit_scale(), rect.center())

    def animated_to_one_to_one(self) -> None:
        """Animated zoom-to-1:1 keeping the current view center.
        Degenerate when the image is smaller than the viewport
        (fit > 1.0 → target clamps up to fit → animation is a no-op)."""
        if not self.hasPhoto():
            return
        target = max(self.fit_scale(), 1.0)
        center = self.mapToScene(self.viewport().rect().center())
        self._animate_to(target, center)

    def setPhoto(self, pixmap: QPixmap | None):
        if pixmap and not pixmap.isNull():
            self._empty = False
            previous_pixmap = self._photo.pixmap()
            self._photo.setPixmap(pixmap)
            if not previous_pixmap or previous_pixmap.isNull() or not previous_pixmap.size() == pixmap.size():
                self.fitInView()
        else:
            self._empty = True
            self._photo.setPixmap(QtGui.QPixmap())
            self.resetZoom()

        if self._mirror_view:
            self.fitMirrorView()

    def resetZoom(self):
        if self.hasPhoto():
            self._zoom = 0
            self._zoomfactor = 1.0
            unity = self.transform().mapRect(QtCore.QRectF(0, 0, 1, 1))
            self.scale(1 / unity.width(), 1 / unity.height())
            # dragMode tracked reactively via scrollbar.rangeChanged.
            self.zoom_changed.emit(self._zoomfactor)

    def zoomPlus(self):
        # Snap-to-1.0 / snap-to-fit on overshoot is handled implicitly
        # by `_apply_scale`'s clamp (target > 1.0 → clamps to 1.0;
        # target < fit → clamps to fit), so the wheel can land
        # precisely on the bounds.
        self._apply_scale(self._zoomfactor * self.ZOOMFACT)

    def zoomMinus(self):
        self._apply_scale(self._zoomfactor / self.ZOOMFACT)

    def wheelEvent(self, event):
        if not self.hasPhoto():
            return
        # Distinguish mouse wheel from trackpad two-finger scroll so
        # mouse wheel zooms (current behavior) and trackpad scrolls
        # pan — matching native macOS / Windows-precision-touchpad
        # behavior in Preview, Photos, etc.
        #
        # `pixelDelta()` is NOT a reliable discriminator on macOS:
        # all wheel events there (mouse OR trackpad) carry pixelDelta
        # because the OS smooths/accelerates everything. Use the
        # scroll *phase* instead — trackpad gestures fire with
        # ScrollBegin/Update/End/Momentum, mouse wheel events fire
        # with NoScrollPhase.
        if event.phase() != Qt.ScrollPhase.NoScrollPhase:
            return super().wheelEvent(event)
        if event.angleDelta().y() > 0:
            self.zoomPlus()
        else:
            self.zoomMinus()

    def event(self, event):
        # Pinch zoom on macOS comes through as a QNativeGestureEvent
        # via the generic event dispatch (no dedicated virtual
        # handler). macOS frames each pinch as a sequence:
        #   BeginNativeGesture → Zoom / Rotate / SmartZoom… → EndNativeGesture
        # We MUST accept every subtype in the sequence, not just
        # ZoomNativeGesture — if begin/end/rotate go unaccepted, the
        # gesture state machine eventually decides this widget isn't
        # interested and stops delivering subsequent zoom events
        # (manifests as "pinches sometimes do nothing").
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                return self._handle_pinch_zoom(event)
            # Claim every other native gesture type so the framework
            # sees consistent handling of the whole sequence.
            event.accept()
            return True
        return super().event(event)

    def eventFilter(self, watched, event):
        # See __init__: scrollbars forward NativeGesture events here
        # so the viewer keeps zooming even when the pinch happens to
        # land on a scrollbar.
        if event.type() == QEvent.Type.NativeGesture:
            return self.event(event)
        return super().eventFilter(watched, event)

    def _handle_pinch_zoom(self, event) -> bool:
        # `value()` is the per-event incremental scale change (~0.01–
        # 0.05 per frame). Apply multiplicatively against current and
        # let `_apply_scale` clamp + sync — AnchorUnderMouse (set in
        # __init__) keeps the point under the cursor stable through
        # the scale, which reads naturally for pinch.
        self._apply_scale(self._zoomfactor * (1.0 + event.value()))
        return True

    def mousePressEvent(self, event):
        if self._photo.isUnderMouse():
            self.photoClicked.emit(QtCore.QPoint(event.pos()))
        super(PhotoViewer, self).mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Toggle Fit ↔ 1:1, centered on the clicked point.

        At fit-scale → jump to 1:1 with the clicked scene-point as the
        viewport center, so the user can inspect a specific region at
        pixel-level without panning. At any other scale → back to fit.

        Degenerate: when the image is smaller than the viewport
        (fit_scale > 1.0), set_absolute_scale clamps 1.0 up to fit, so
        the 'jump to 1:1' branch becomes a visual no-op — fine, there's
        nothing meaningful to toggle to in that case."""
        if not self.hasPhoto():
            return super().mouseDoubleClickEvent(event)
        fit = self.fit_scale()
        if abs(self._zoomfactor - fit) < 1e-3:
            scene_pt = self.mapToScene(event.pos())
            self._animate_to(max(fit, 1.0), scene_pt)
        else:
            self.animated_fit_in_view()
        event.accept()

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
