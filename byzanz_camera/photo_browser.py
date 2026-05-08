import os
import re
from os import listdir
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QEvent, QFileSystemWatcher, QPoint, Qt, QThreadPool, pyqtSignal, QMutex, \
    QMutexLocker, QRectF, QSize
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap,
    QPixmapCache, QImage, QResizeEvent,
)
from PyQt6.QtWidgets import (
    QGraphicsView, QGroupBox, QListView, QListWidget, QListWidgetItem,
    QMenu, QStyledItemDelegate, QVBoxLayout, QWidget,
)
from PyQt6.uic import loadUi

from .load_image_worker import DecodeMode, LoadImageWorker, LoadImageWorkerResult, SUPPORTED_EXTENSIONS
from .photo_viewer import PhotoViewer
from .spinner import Spinner

from .helpers import get_ui_path


class _ViewStatePill(QWidget):
    """Custom-painted pill badge for the viewer mode indicator.

    QSS-styled QLabel proved unreliable across Qt6/macOS — `border-radius`
    + transparent rgba background combinations either render inconsistently
    or silently drop the chrome entirely. A self-painting widget bypasses
    QSS entirely, which is the only reliable way to get the rounded dark
    pill look across platforms.
    """

    PAD_X = 12
    PAD_Y = 6
    RADIUS = 11

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._bg = QColor(15, 23, 42, 220)         # semi-transparent slate-900
        self._fg = QColor("white")
        self._border_color: QColor | None = None   # set per-state, None = no border
        self._border_w = 1.5
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def set_border_color(self, color: str | QColor | None) -> None:
        """Set the pill's outline color (matches the state's accent).
        Pass None to drop the border."""
        new = QColor(color) if color and not isinstance(color, QColor) else color
        if (self._border_color is None) == (new is None) and new == self._border_color:
            return
        self._border_color = new
        self.update()

    def setText(self, text: str) -> None:
        if self._text == text:
            return
        self._text = text
        self.adjustSize()
        self.update()

    def _font(self) -> QFont:
        font = QFont()
        font.setBold(True)
        font.setPointSize(9)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
        return font

    def sizeHint(self) -> QSize:
        if not self._text:
            return QSize(0, 0)
        fm = QFontMetrics(self._font())
        return QSize(
            fm.horizontalAdvance(self._text) + 2 * self.PAD_X,
            fm.height() + 2 * self.PAD_Y,
        )

    def paintEvent(self, event) -> None:
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Inset the rect by half the border width so the stroke isn't
        # cropped by the widget edges.
        bw = self._border_w if self._border_color is not None else 0
        half = bw / 2
        rect = QRectF(half, half, self.width() - bw, self.height() - bw)

        # Fill: semi-transparent dark
        p.setBrush(QBrush(self._bg))
        if self._border_color is not None:
            p.setPen(QPen(self._border_color, bw))
        else:
            p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, self.RADIUS, self.RADIUS)

        # Centered text
        p.setFont(self._font())
        p.setPen(QPen(self._fg))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


def get_file_index(file_path) -> Optional[int]:
    basename = os.path.splitext(file_path)[0]
    numbers_in_basename = re.findall(r'\d+', basename)
    return int(numbers_in_basename[-1]) if numbers_in_basename else None


class ImageFileListItem(QListWidgetItem):
    def __init__(self, path: str, thumbnail: QPixmap):
        super().__init__()
        self.path: str = path
        self.file_name = Path(path).name
        self.index = get_file_index(self.file_name)
        self.thumbnail: QPixmap = thumbnail

    def __lt__(self, other):
        return self.index < other.index

    def data(self, role: Qt.ItemDataRole):
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnail
        if role == Qt.ItemDataRole.UserRole:
            # Lets a custom delegate read the item directly from the model index
            # without having to reach into the parent QListWidget.
            return self

        return super().data(role)

class PhotoBrowser(QWidget):
    directory_loaded = pyqtSignal(str)
    image_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        loadUi(get_ui_path('ui/photo_browser.ui'), self)

        self.__fileSystemWatcher = QFileSystemWatcher()
        self.__threadpool = QThreadPool()
        self.__threadpool.setMaxThreadCount(4)
        self.__num_images_to_load = 0

        # Generation token — bumps on every open/close. Each LoadImageWorker
        # captures the gen at queue time; results from a stale gen are
        # silently dropped at __on_image_loaded. Lets us avoid blocking on
        # `threadpool.waitForDone()` in close_directory (which made bucket
        # switches feel laggy) without leaking thumbnails into the wrong
        # bucket's list when the user switches mid-load.
        self.__generation = 0

        self.__currentPath: str = None
        self.__currentFileSet: set[str] = set()
        self.__ctx_menu_provider: Optional[Callable[[ImageFileListItem], Optional[QMenu]]] = None

        self.photo_viewer: PhotoViewer = self.findChild(QWidget, "photoViewer")
        self.image_file_list: QListWidget = self.findChild(QListWidget, "imageFileList")
        self.viewer_container: QWidget = self.findChild(QWidget, "viewerContainer")

        self.__fileSystemWatcher.directoryChanged.connect(self.__load_directory)

        self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

        self.spinner = Spinner(self.viewer_container, Spinner.m_light_color)
        self.spinner.isAnimated = False
        self.__center_spinner_over_photo_viewer()
        self.resize(self.size())

        self.__mutex = QMutex()

    # ---- Extension points for subclasses --------------------------------

    def set_item_delegate(self, delegate: QStyledItemDelegate) -> None:
        """Install a custom delegate for thumbnail items. Lets subclasses
        overlay decorations (e.g. ★ chosen-take marker) without touching
        the inner list widget directly.
        """
        self.image_file_list.setItemDelegate(delegate)

    def set_context_menu_provider(
        self,
        provider: Callable[[ImageFileListItem], Optional[QMenu]],
    ) -> None:
        """Register a callable that, given the right-clicked item, returns a
        QMenu to show (or None to skip). Wires the context-menu policy and
        signal once on first call.
        """
        first_install = self.__ctx_menu_provider is None
        self.__ctx_menu_provider = provider
        if first_install:
            self.image_file_list.setContextMenuPolicy(
                Qt.ContextMenuPolicy.CustomContextMenu
            )
            self.image_file_list.customContextMenuRequested.connect(
                self.__on_context_menu_requested
            )

    def repaint_items(self) -> None:
        """Trigger a repaint of visible items. Call after external state
        affecting item decoration changes (e.g. chosen-take swap)."""
        self.image_file_list.viewport().update()

    # ---- view-state indicator (corner pill + viewer border tint) -----

    # State strings:
    #   "live"    — live frames streaming from the camera
    #   "paused"  — live view paused, last frame frozen on screen
    #   "preview" — user-selected capture (filename in the pill)
    #   "empty"   — nothing meaningful to show
    # Border + accent colors per state. "Live" uses cyan instead of red
    # so it doesn't visually fight IR's amber identity (red is also a
    # destructive/error color in our palette so we want to keep it for
    # actual error states only).
    _VIEW_STATE_BORDERS = {
        "live":    "1.5px solid #06b6d4",   # cyan-500
        "paused":  "1px dashed #cbd5e1",
        "preview": "1.5px solid #94a3b8",
        "empty":   "1px solid #e2e8f0",
    }
    _LIVE_DOT_COLOR    = "#06b6d4"   # cyan-500 (same as border for cohesion)
    _PAUSED_ICON_COLOR = "#fbbf24"   # amber-400

    def enable_view_state_indicator(self) -> None:
        """Add a corner-pill + border-tint indicator to the viewer area.
        Call `set_view_state(state, label="")` to update.

        Idempotent — safe to call from a subclass __init__."""
        if getattr(self, "_view_state_pill", None) is not None:
            return

        self._view_state: str = "empty"
        self._view_state_label: str = ""

        # Custom-painted pill — see _ViewStatePill comment for why we
        # don't use QLabel + QSS here.
        pill = _ViewStatePill(self.viewer_container)
        self._view_state_pill = pill

        # Border goes on the QGraphicsView itself, not viewer_container —
        # PhotoViewer fills the container so a border on the container
        # would be hidden behind the QGraphicsView's own paint area.
        self.photo_viewer.setObjectName("photoViewer")
        # Track viewport resize so the pill re-anchors when the user
        # resizes the window or scrollbars appear/disappear.
        self.photo_viewer.viewport().installEventFilter(self)

        self._refresh_view_state_indicator()

    def set_view_state(self, state: str, label: str = "") -> None:
        """Update the indicator. `state` ∈ {live, paused, preview, empty}.
        `label` is shown inside the pill for "preview" (typically the
        filename); ignored for other states."""
        if state not in self._VIEW_STATE_BORDERS:
            return
        self._view_state = state
        self._view_state_label = label
        self._refresh_view_state_indicator()

    def _refresh_view_state_indicator(self) -> None:
        if getattr(self, "_view_state_pill", None) is None:
            return
        state = self._view_state
        label = self._view_state_label

        # Border tint on the QGraphicsView (= the visible image area).
        border = self._VIEW_STATE_BORDERS[state]
        self.photo_viewer.setStyleSheet(
            f"QGraphicsView#photoViewer {{ border: {border}; border-radius: 3px; }}"
        )

        # Pill visibility / content + accent border per state.
        pill = self._view_state_pill
        if state == "empty":
            pill.hide()
            return

        # Per-state text + border color (matching the viewer border tint
        # so the pill outline reads as part of the same indicator).
        if state == "live":
            pill.setText("● LIVE")
            pill.set_border_color(self._LIVE_DOT_COLOR)
        elif state == "paused":
            pill.setText("⏸ PAUSED")
            pill.set_border_color(self._PAUSED_ICON_COLOR)
        elif state == "preview":
            pill.setText(f"📷 {label}" if label else "📷 Preview")
            pill.set_border_color("#94a3b8")  # neutral grey

        pill.adjustSize()
        pill.show()
        pill.raise_()
        self._reposition_view_state_pill()

    def _reposition_view_state_pill(self) -> None:
        pill = getattr(self, "_view_state_pill", None)
        if pill is None or not pill.isVisible():
            return
        inset = 12
        # Anchor to the QGraphicsView's viewport (scroll body) so the pill
        # always sits inside the visible image area, never overlapping a
        # vertical scrollbar.
        viewport = self.photo_viewer.viewport()
        anchor_right = (
            self.photo_viewer.x() + viewport.x() + viewport.width()
        )
        pill.move(
            max(0, anchor_right - pill.width() - inset),
            self.photo_viewer.y() + viewport.y() + inset,
        )

    def eventFilter(self, obj, event):
        if (
            obj is getattr(self.photo_viewer, "viewport", lambda: None)()
            and event.type() == QEvent.Type.Resize
        ):
            self._reposition_view_state_pill()
        return super().eventFilter(obj, event)

    # ---- loupe layout ------------------------------------------------

    def use_loupe_layout(self) -> None:
        """Switch from the default side-by-side layout (list left, viewer right)
        to a Lightroom-style loupe layout: big viewer on top, horizontal
        filmstrip of thumbnails below. Strips the group-box titles and gives
        the workspace as much vertical room as possible.

        Idempotent in spirit but intended to be called once at construction
        — there's no `unuse_loupe_layout`. Safe to call from a subclass's
        __init__ after `super().__init__`.
        """
        list_box = self.image_file_list.parentWidget()
        viewer_box = self.viewer_container.parentWidget()

        # Strip "Bilder" / "Vorschau" titles — they read as section labels in
        # the side-by-side layout but are visual noise in the loupe form.
        for box in (list_box, viewer_box):
            if isinstance(box, QGroupBox):
                box.setTitle("")

        # Detach old layout and replace with vertical: viewer (top, stretches)
        # then list (bottom, fixed height). Ownership transfer follows the
        # Qt idiom of parking the old layout on a throwaway widget so it gets
        # cleaned up when that widget falls out of scope.
        old_layout = self.layout()
        while old_layout.count():
            old_layout.takeAt(0)
        QWidget().setLayout(old_layout)

        new_layout = QVBoxLayout(self)
        new_layout.setContentsMargins(0, 0, 0, 0)
        new_layout.setSpacing(8)
        new_layout.addWidget(viewer_box, 1)
        new_layout.addWidget(list_box)

        # Filmstrip box: fixed height, no width cap. The original .ui sets
        # maximumWidth=250 for the side-by-side flavor — undo that. The
        # default text-below layout is gone; height only has to fit the
        # thumb itself plus a little padding (subclasses are expected to
        # paint any caption as a delegate overlay on top of the thumb).
        list_box.setMaximumWidth(16777215)
        list_box.setMaximumHeight(160)
        list_box.setMinimumHeight(140)

        # List flow → horizontal: thumbnails march left-to-right, no wrap.
        self.image_file_list.setFlow(QListView.Flow.LeftToRight)
        self.image_file_list.setWrapping(False)
        self.image_file_list.setHorizontalScrollMode(
            QListView.ScrollMode.ScrollPerPixel
        )
        self.image_file_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.image_file_list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Thumb-only tiles: thumb fills the cell so a delegate overlay sits
        # edge-to-edge on the thumbnail. The side-by-side layout shows
        # filename+EXIF below the thumb; the loupe form expects subclasses
        # to overlay caption text via the delegate.
        self.image_file_list.setIconSize(QSize(120, 120))
        self.image_file_list.setGridSize(QSize(124, 124))
        self.image_file_list.setSpacing(4)

    # ---- Existing public API --------------------------------------------

    def get_scene(self):
        return self.photo_viewer.getScene()

    def set_mirror_graphics_view(self, view: QGraphicsView):
        self.photo_viewer.setMirrorView(view)

    def open_directory(self, dir_path):
        if self.__currentPath:
            self.close_directory()

        self.__currentPath = dir_path
        self.start_watching()
        self.__load_directory()

    def start_watching(self):
        # print("START watching " + self.__currentPath)
        self.__fileSystemWatcher.addPath(self.__currentPath)

    def close_directory(self):
        # Bump generation FIRST so any worker results that arrive after
        # this point (queued in the event loop, or completing on the
        # threadpool right now) see a stale gen and skip themselves.
        self.__generation += 1
        self.stop_watching()
        self.__currentPath = None
        self.__currentFileSet.clear()
        # Drop queued (not-yet-started) workers from the pool. Running
        # workers will complete on their own; their finished signals
        # are dropped via the gen check in __on_image_loaded — we
        # deliberately do NOT call waitForDone() so the close returns
        # promptly and the UI stays responsive on rapid bucket switches.
        self.__threadpool.clear()
        self.__num_images_to_load = 0
        self._reset_displayed_state()

    def _reset_displayed_state(self) -> None:
        """Reset every piece of UI that mirrors "what bucket is displayed".
        Single chokepoint so a new piece of displayed state can't be added
        without showing up here. Called from close_directory.

        Contract — anything that represents bucket-bound display content
        MUST be reset here. Pre-existing examples:
            - filmstrip items (image_file_list)
            - main viewer image (photo_viewer)
            - shared decoded-pixmap cache
            - load spinner (left running by stale-skipping workers; close
              ends the load so we stop it explicitly)
            - corner pill / border tint (when the indicator is enabled)
        """
        self.image_file_list.clear()
        self.photo_viewer.setPhoto(None)
        QPixmapCache.clear()
        self.spinner.stopAnimation()
        if hasattr(self, "_view_state_pill"):
            self.set_view_state("empty")

    def stop_watching(self):
        # No-op when nothing was being watched — Qt's removePath emits a
        # warning on an empty/None path. Reachable when bind_object(None)
        # is called on a browser that never opened a directory.
        if not self.__currentPath:
            return
        self.__fileSystemWatcher.removePath(self.__currentPath)

    def num_files(self) -> int:
        return self.image_file_list.count()

    def current_file_name(self) -> Optional[str]:
        """The basename of the currently-selected thumbnail, or None if
        the list is empty / nothing is selected."""
        item = self.image_file_list.currentItem()
        if item is None or not isinstance(item, ImageFileListItem):
            return None
        return item.file_name

    def files(self):
        return [self.image_file_list.item(row).path for row in range(self.image_file_list.count())]

    def last_index(self) -> int:
        image_count = self.image_file_list.count()
        if image_count > 0:
            return self.image_file_list.item(image_count - 1).index

        return 0

    def resizeEvent(self, event: QResizeEvent):
        self.__center_spinner_over_photo_viewer()

    def showEvent(self, event):
        # By the time the widget is shown, Qt has laid out the inner container,
        # so viewer_container.width()/height() return real numbers (vs 0 at __init__).
        super().showEvent(event)
        self.__center_spinner_over_photo_viewer()

    def show_preview(self, image: QImage | None):
        # NOTE: this widget no longer touches `image_file_list.setEnabled()`.
        # "Should clicks be allowed during live view?" is a UX policy that
        # belongs to the consumer (e.g. papyri auto-pauses live view on
        # selection via the `image_selected` signal). PhotoBrowser is now
        # purely a view of (file list, viewer) without a coupled lock.
        if not image:
            self.photo_viewer.setPhoto(None)
            # re-show the previously selected image, if any (so the viewer
            # doesn't go blank when live view stops without an active selection)
            selected_image_index = self.image_file_list.currentIndex()
            if selected_image_index:
                item = self.image_file_list.item(selected_image_index.row())
                self.__on_select_image_file(item)
            return

        self.photo_viewer.setPhoto(QPixmap.fromImage(image))
        self.photo_viewer.fitInView()

    def __load_directory(self):
        print("Load directory: " + self.__currentPath)
        if os.path.isdir(self.__currentPath):
            new_files = [f for f in listdir(self.__currentPath)
                         if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS and get_file_index(f) is not None]
        else:
            # Watched path is gone (deleted in Finder, or never existed).
            # Treat as empty so the add/remove diffing below clears any
            # stale items rather than leaving the list lying.
            new_files = []

        new_fileset = set(new_files)
        added_files = new_fileset - self.__currentFileSet
        removed_files = self.__currentFileSet - new_fileset

        if not added_files and not removed_files:
            self.directory_loaded.emit(self.__currentPath)

        if added_files:
            # self.__threadpool.waitForDone()
            # self.stop_watching()
            for f in added_files:
                # Browser thumbnails: use the embedded JPEG preview for RAWs
                # (fast). The full decode is reserved for the viewer click flow.
                self.__load_image(f, self.__add_image_item, DecodeMode.THUMB)

        for f in removed_files:
            for i in range(self.image_file_list.count()):
                item = self.image_file_list.item(i)
                if isinstance(item, ImageFileListItem):
                    if item.file_name == f:
                        self.image_file_list.takeItem(i)
                        del item




        self.__currentFileSet = new_fileset

    def __on_directory_loaded(self):
        # self.start_watching()
        self.directory_loaded.emit(self.__currentPath)
        # image_count = self.image_file_list.count()
        # if image_count > 0:
        #     self.image_file_list.setCurrentItem(self.image_file_list.item(image_count - 1))

        # just in case there have been changes while loading the files
        self.__load_directory()

    def __load_image(self, file_name: str, on_finished_callback: Callable,
                     decode_mode: DecodeMode = DecodeMode.FULL):
        self.__num_images_to_load +=1

        worker = LoadImageWorker(
            os.path.join(self.__currentPath, file_name),
            include_thumbnail=True,
            thumbnail_size=200,
            decode_mode=decode_mode,
        )
        # Capture generation at queue time; checked at receive time. Each
        # call's `gen` is its own local, so each lambda closes over a
        # distinct value (Python closure-by-reference, but the variable
        # is re-bound per call so each lambda gets the snapshot).
        gen = self.__generation
        worker.signals.finished.connect(
            lambda result: self.__on_image_loaded(result, on_finished_callback, gen)
        )

        self.spinner.startAnimation()
        self.__threadpool.start(worker)

    def __on_image_loaded(self, result: LoadImageWorkerResult,
                          on_finished_callback: Callable, gen: int):
        # Stale guard — a previous open_directory's worker just finished
        # but we've since closed and (maybe) re-opened. Drop the result;
        # don't touch the counter (close_directory already reset it for
        # the new gen) or the spinner (current-gen workers will stop it
        # when they finish).
        if gen != self.__generation:
            return
        # Caching is the caller's decision (e.g. only the viewer flow caches —
        # the directory-thumbnail flow used to evict view pixmaps from the cache).
        on_finished_callback(result)

        self.__num_images_to_load -= 1
        if self.__num_images_to_load == 0:
            self.__on_directory_loaded()

        self.spinner.stopAnimation()

    def __on_select_image_file(self, item: ImageFileListItem):
        if item:
            file_path = item.path
            cached_image = QPixmapCache.find(file_path)
            if cached_image:
                print("cache hit")
                self.photo_viewer.setPhoto(cached_image)
            else:
                print("cache miss")
                self.__load_image(file_path, self.__show_and_cache)
            self.image_selected.emit(file_path)
        else:
            self.photo_viewer.setPhoto(None)

    def __show_and_cache(self, result: LoadImageWorkerResult):
        pixmap = QPixmap.fromImage(result.image)
        QPixmapCache.insert(result.path, pixmap)
        self.photo_viewer.setPhoto(pixmap)

    def __add_image_item(self, image_worker_result: LoadImageWorkerResult):
        list_item = ImageFileListItem(image_worker_result.path, image_worker_result.thumbnail)

        exposure_time = image_worker_result.exif["ExposureTime"].real
        f_number = image_worker_result.exif["FNumber"]
        list_item.setText("%s\nf/%s | %s" % (list_item.file_name, f_number, exposure_time))

        # Only add the item to the list if a directory is still open. This function
        # can be called asynchronously from a thread so the directory could have been
        # closed in the meantime.
        with QMutexLocker(self.__mutex):
            if self.__currentPath:
                self.image_file_list.addItem(list_item)
                self.image_file_list.sortItems()
                self.image_file_list.scrollToBottom()

                self.image_file_list.currentItemChanged.disconnect()
                self.image_file_list.setCurrentItem(list_item)
                self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

        image_path = image_worker_result.path
        pixmap: QPixmap = QPixmapCache.find(image_path) or QPixmap.fromImage(image_worker_result.image)
        self.photo_viewer.setPhoto(pixmap)
        if self.image_file_list.indexFromItem(list_item).row() == 0:
            self.photo_viewer.fitInView()



    def __center_spinner_over_photo_viewer(self):
        size = 120
        spinner_x = max(0, (self.viewer_container.width() - size) // 2)
        spinner_y = max(0, (self.viewer_container.height() - size) // 2)
        self.spinner.setGeometry(spinner_x, spinner_y, size, size)
        self.spinner.raise_()

    def __on_context_menu_requested(self, position: QPoint):
        item = self.image_file_list.itemAt(position)
        if item is None or self.__ctx_menu_provider is None:
            return
        menu = self.__ctx_menu_provider(item)
        if menu is None:
            return
        menu.exec(self.image_file_list.viewport().mapToGlobal(position))
