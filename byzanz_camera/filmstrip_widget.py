"""FilmstripWidget — directory-bound thumbnail strip with async loading.

Owns a directory binding (path + FS watcher), an async thumbnail decoder
(QThreadPool + LoadImageWorker), and a horizontal QListWidget showing the
captures in that directory. Emits signals when items are clicked, decoded
(full-image), or when the directory load completes / closes.

Companion to ViewerWidget. The strip never reaches into a viewer; it just
emits image_decoded(path, pixmap) and lets the parent layout's coordinator
wire it to a viewer (or drop it on the floor).

Subclasses extend behavior via:
  - set_item_delegate(delegate)        custom thumbnail painting
  - set_context_menu_provider(fn)      per-item right-click menu
  - repaint_items()                    request a repaint after external
                                       state change (e.g. chosen-take swap)
"""
from __future__ import annotations

import os
from os import listdir
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import (
    QFileSystemWatcher, QMutex, QMutexLocker, QPoint, QSize, Qt,
    QThreadPool, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QPixmap, QPixmapCache
from PyQt6.QtWidgets import (
    QListView, QListWidget, QListWidgetItem, QMenu, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

from .load_image_worker import (
    DecodeMode, LoadImageWorker, LoadImageWorkerResult, SUPPORTED_EXTENSIONS,
)


# ---- model items -------------------------------------------------------

def get_file_index(file_path: str) -> Optional[int]:
    """Extract the trailing integer in a filename's stem (e.g. `..._001`
    → 1). Returns None if no digits found — those files are skipped."""
    import re
    basename = os.path.splitext(file_path)[0]
    numbers_in_basename = re.findall(r'\d+', basename)
    return int(numbers_in_basename[-1]) if numbers_in_basename else None


class ImageFileListItem(QListWidgetItem):
    """One thumbnail in the strip. Holds the absolute path, the thumbnail
    pixmap, and the parsed numeric index for sorting. Custom delegates can
    read this via `index.data(Qt.ItemDataRole.UserRole)`."""

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
            return self
        return super().data(role)


# ---- the widget --------------------------------------------------------

class FilmstripWidget(QWidget):
    """Directory-bound thumbnail strip.

    Owns the directory binding, the FS watcher, the thumbnail decode
    threadpool, and the horizontal QListWidget. Emits decoded images on
    click (and on each thumbnail add during initial load, preserving the
    "latest-loaded shows in the viewer" UX without coupling to a viewer).

    Layout: a single QListWidget set to LeftToRight flow with a fixed
    height (140-160px); thumbnails fill cells edge-to-edge so subclass
    delegates can overlay caption / chosen-mark badges.
    """

    # ---- signals -------------------------------------------------------

    # USER click only. Auto-selection during initial load is suppressed
    # via disconnect/reconnect in __add_image_item (H22), so this fires
    # only when the user actually clicks a thumb.
    image_selected = pyqtSignal(str)            # path

    # "Display this in the viewer." Emitted when:
    #   - a thumbnail is added during initial load (auto-show)
    #   - user clicks a thumb (cache hit OR full-decode complete)
    # Stale-guarded against rapid clicks via currentItem comparison.
    image_decoded = pyqtSignal(str, QPixmap)    # path, pixmap

    # Selection cleared (e.g. last item removed). Viewer should clear.
    image_cleared = pyqtSignal()

    # Async thumbnail load complete. Emitted exactly once per
    # open_directory call, AFTER the receiver chain that triggered the
    # load has unwound (deferred via QTimer.singleShot when no diff).
    directory_loaded = pyqtSignal(str)          # path

    # close_directory exit signal — viewer subscribes to clear itself.
    directory_closed = pyqtSignal(str)          # path that was closed

    # ---- construction --------------------------------------------------

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.image_file_list = QListWidget(self)
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
        # Thumb-only tiles: thumb fills the cell so a delegate overlay
        # sits edge-to-edge on the thumbnail.
        self.image_file_list.setIconSize(QSize(120, 120))
        self.image_file_list.setGridSize(QSize(124, 124))
        self.image_file_list.setSpacing(4)
        layout.addWidget(self.image_file_list)

        # Sensible defaults; consumer can override via setMinimumHeight etc.
        self.setMinimumHeight(140)
        self.setMaximumHeight(160)

        # Directory binding state
        self.__currentPath: str | None = None
        self.__currentFileSet: set[str] = set()

        # Async thumbnail loading
        self.__threadpool = QThreadPool()
        self.__threadpool.setMaxThreadCount(4)
        self.__num_images_to_load = 0
        # Generation token: bumps on every open/close so worker results
        # from a superseded directory get dropped at __on_image_loaded.
        # Lets close_directory return promptly without blocking on
        # waitForDone() — running workers complete on their own and
        # their results are silently discarded when stale.
        self.__generation = 0

        # FS watcher: fires __load_directory on directory changes
        # (new captures land via the camera worker → reflected here).
        self.__fileSystemWatcher = QFileSystemWatcher()
        self.__fileSystemWatcher.directoryChanged.connect(self.__load_directory)

        # Mutex protects __add_image_item against concurrent calls from
        # multiple worker threads completing thumbnail decodes.
        self.__mutex = QMutex()

        # Subclass extension points
        self.__ctx_menu_provider: Optional[Callable[[ImageFileListItem], Optional[QMenu]]] = None

        self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

    # ---- public API ----------------------------------------------------

    def open_directory(self, dir_path: str) -> None:
        """Open a new directory: close any currently-open one, start
        watching, kick off the initial async thumb load."""
        if self.__currentPath:
            self.close_directory()
        self.__currentPath = dir_path
        self.__fileSystemWatcher.addPath(self.__currentPath)
        self.__load_directory()

    def close_directory(self) -> None:
        """Close the currently-bound directory. Bumps the generation
        token (so any in-flight worker results are dropped), stops
        watching, clears the displayed strip, and emits directory_closed
        so consumers (e.g. the viewer) can reset themselves."""
        # Bump generation FIRST so any worker results that arrive after
        # this point see a stale gen and skip themselves.
        self.__generation += 1
        path = self.__currentPath
        self.__stop_watching()
        self.__currentPath = None
        self.__currentFileSet.clear()
        # Drop queued (not-yet-started) workers from the pool. Running
        # workers will complete on their own; their finished signals
        # are dropped via the gen check in __on_image_loaded — we
        # deliberately do NOT call waitForDone() so close returns
        # promptly and the UI stays responsive on rapid switches.
        self.__threadpool.clear()
        self.__num_images_to_load = 0
        self._reset_displayed_state()
        if path is not None:
            self.directory_closed.emit(path)

    def current_file_name(self) -> Optional[str]:
        """Basename of the currently-selected thumbnail, or None."""
        item = self.image_file_list.currentItem()
        if item is None or not isinstance(item, ImageFileListItem):
            return None
        return item.file_name

    def current_path(self) -> Optional[str]:
        """The currently-bound directory path, or None."""
        return self.__currentPath

    def num_files(self) -> int:
        return self.image_file_list.count()

    def files(self) -> list[str]:
        return [self.image_file_list.item(row).path
                for row in range(self.image_file_list.count())]

    def last_index(self) -> int:
        n = self.image_file_list.count()
        return self.image_file_list.item(n - 1).index if n > 0 else 0

    # ---- subclass / extension API --------------------------------------

    def set_item_delegate(self, delegate: QStyledItemDelegate) -> None:
        """Install a custom delegate for thumbnail items (e.g. to overlay
        a chosen-take ★ marker)."""
        self.image_file_list.setItemDelegate(delegate)

    def set_context_menu_provider(
        self,
        provider: Callable[[ImageFileListItem], Optional[QMenu]],
    ) -> None:
        """Register a callable that, given the right-clicked item, returns
        a QMenu to show (or None to skip). Wires the context-menu policy
        and signal once on first call."""
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
        """Trigger a repaint of visible items (after external state
        affecting decoration changes, e.g. chosen-take swap)."""
        self.image_file_list.viewport().update()

    # ---- reset contract -----------------------------------------------

    def _reset_displayed_state(self) -> None:
        """Reset every piece of UI that mirrors "what directory is shown".
        Single chokepoint — new state added that represents directory-bound
        content MUST be reset here. Called from close_directory.

        Pre-existing entries:
            - filmstrip items (image_file_list)
            - shared decoded-pixmap cache (cleared so next directory's
              clicks don't get cached results from the previous one)
        """
        self.image_file_list.clear()
        QPixmapCache.clear()

    def __stop_watching(self) -> None:
        """No-op when nothing was being watched — Qt's removePath emits
        a warning on an empty/None path. Reachable when close_directory
        is called on a strip that never opened a directory."""
        if not self.__currentPath:
            return
        self.__fileSystemWatcher.removePath(self.__currentPath)

    # ---- async load -----------------------------------------------------

    def __load_directory(self) -> None:
        """Diff disk against the last-known fileset. Queue thumbnail
        decodes for new files; remove items for vanished files. Emit
        directory_loaded if there's nothing to load (and any time
        __num_images_to_load reaches 0 in __on_image_loaded)."""
        if os.path.isdir(self.__currentPath):
            new_files = [
                f for f in listdir(self.__currentPath)
                if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS
                and get_file_index(f) is not None
            ]
        else:
            # Watched path is gone (deleted, or never existed). Treat as
            # empty so the diff below clears any stale items rather than
            # leaving them lying around.
            new_files = []

        new_fileset = set(new_files)
        added_files = new_fileset - self.__currentFileSet
        removed_files = self.__currentFileSet - new_fileset

        if not added_files and not removed_files:
            # Defer to the event loop so the emit lands AFTER whatever
            # receiver chain triggered this load completes — synchronous
            # re-entry would hit slots whose connections are still being
            # set up by later receivers in the same chain.
            path = self.__currentPath
            QTimer.singleShot(0, lambda: self.directory_loaded.emit(path))

        if added_files:
            for f in added_files:
                # Browser thumbnails: use the embedded JPEG preview for
                # RAWs (fast). The full decode is reserved for click.
                self.__load_image(f, self.__add_image_item, DecodeMode.THUMB)

        for f in removed_files:
            for i in range(self.image_file_list.count()):
                item = self.image_file_list.item(i)
                if isinstance(item, ImageFileListItem) and item.file_name == f:
                    self.image_file_list.takeItem(i)
                    del item
                    break

        self.__currentFileSet = new_fileset

    def __load_image(
        self,
        file_name: str,
        on_finished_callback: Callable,
        decode_mode: DecodeMode = DecodeMode.FULL,
    ) -> None:
        """Queue an async decode worker. Captures the current generation
        token so __on_image_loaded can drop stale results."""
        self.__num_images_to_load += 1

        worker = LoadImageWorker(
            os.path.join(self.__currentPath, file_name),
            include_thumbnail=True,
            thumbnail_size=200,
            decode_mode=decode_mode,
        )
        # Each call's `gen` is its own local — Python closures capture by
        # reference but the variable is re-bound per call so each lambda
        # closes over a distinct value.
        gen = self.__generation
        worker.signals.finished.connect(
            lambda result: self.__on_image_loaded(result, on_finished_callback, gen)
        )
        self.__threadpool.start(worker)

    def __on_image_loaded(
        self,
        result: LoadImageWorkerResult,
        on_finished_callback: Callable,
        gen: int,
    ) -> None:
        """Receive a worker result. Drop if stale (a previous open's
        worker just finished but we've since closed and maybe re-opened)."""
        if gen != self.__generation:
            return
        on_finished_callback(result)
        self.__num_images_to_load -= 1
        if self.__num_images_to_load == 0:
            # Async load complete — emit (deferred via QTimer for the same
            # reason __load_directory's no-diff branch defers).
            path = self.__currentPath
            QTimer.singleShot(0, lambda: self.directory_loaded.emit(path))
            # Re-scan in case files arrived during loading.
            self.__load_directory()

    def __add_image_item(self, image_worker_result: LoadImageWorkerResult) -> None:
        """Create a list item for this file and add it (auto-selects last,
        suppressing the currentItemChanged signal so image_selected only
        fires on USER click — H22). Emits image_decoded so the viewer
        shows the latest-loaded image, preserving the as-it-loads UX."""
        list_item = ImageFileListItem(
            image_worker_result.path, image_worker_result.thumbnail
        )

        exposure_time = image_worker_result.exif["ExposureTime"].real
        f_number = image_worker_result.exif["FNumber"]
        list_item.setText("%s\nf/%s | %s" % (
            list_item.file_name, f_number, exposure_time
        ))

        # Add the item only if a directory is still open (this callback
        # fires from a worker thread completion; the directory may have
        # been closed in the meantime — extra safety on top of the
        # generation-token gate in __on_image_loaded).
        with QMutexLocker(self.__mutex):
            if self.__currentPath:
                self.image_file_list.addItem(list_item)
                self.image_file_list.sortItems()
                self.image_file_list.scrollToBottom()
                self.image_file_list.currentItemChanged.disconnect()
                self.image_file_list.setCurrentItem(list_item)
                self.image_file_list.currentItemChanged.connect(
                    self.__on_select_image_file
                )

        # Emit for the viewer: cache hit if we somehow have it, else use
        # the thumb-mode pixmap from the worker result (good enough for
        # initial display; full decode happens on click).
        image_path = image_worker_result.path
        pixmap = (QPixmapCache.find(image_path)
                  or QPixmap.fromImage(image_worker_result.image))
        self.image_decoded.emit(image_path, pixmap)

    # ---- selection -----------------------------------------------------

    def __on_select_image_file(self, item: ImageFileListItem | None) -> None:
        """Selection changed — either USER click (most common) or
        programmatic via setCurrentItem (suppressed to NOT fire
        image_selected per H22, but the disconnect/reconnect dance in
        __add_image_item means this slot literally isn't called for those)."""
        if item is None:
            self.image_cleared.emit()
            return
        file_path = item.path
        cached_image = QPixmapCache.find(file_path)
        if cached_image:
            self.image_decoded.emit(file_path, cached_image)
        else:
            self.__load_image(file_path, self.__show_and_cache)
        self.image_selected.emit(file_path)

    def __show_and_cache(self, result: LoadImageWorkerResult) -> None:
        """Full-decode result for a clicked thumb. Cache unconditionally
        (a future click on the same thumb is instant); display only if
        this result still matches the user's current selection — guards
        rapid thumb clicks where an earlier worker finishes after a
        later one's selection."""
        pixmap = QPixmap.fromImage(result.image)
        QPixmapCache.insert(result.path, pixmap)
        current = self.image_file_list.currentItem()
        if isinstance(current, ImageFileListItem) and current.path == result.path:
            self.image_decoded.emit(result.path, pixmap)

    # ---- context menu --------------------------------------------------

    def __on_context_menu_requested(self, position: QPoint) -> None:
        item = self.image_file_list.itemAt(position)
        if item is None or self.__ctx_menu_provider is None:
            return
        menu = self.__ctx_menu_provider(item)
        if menu is None:
            return
        menu.exec(self.image_file_list.viewport().mapToGlobal(position))
