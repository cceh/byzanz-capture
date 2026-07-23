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
import time
from os import listdir
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import (
    QFileSystemWatcher, QMutex, QMutexLocker, QPoint, QRect, QRectF,
    QSize, Qt, QThreadPool, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QIcon, QImage, QLinearGradient, QPainter, QPixmap, QPixmapCache,
)
from PyQt6.QtWidgets import (
    QFrame, QListView, QListWidget, QListWidgetItem, QMenu,
    QSizePolicy, QStyle, QStyleOptionViewItem, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .load_image_worker import (
    ImageMode, JPEG_EXTENSIONS, LoadImageWorker, LoadImageWorkerResult,
    SUPPORTED_EXTENSIONS,
)


# ---- layout constants --------------------------------------------------

# Thumbnail (the painted icon, 3:2 aspect to match camera native orientation).
THUMB_WIDTH = 120
THUMB_HEIGHT = 80

# Visible gap between adjacent thumbnails and overlays. Manufactured via
# gridSize > iconSize: each cell has THUMB_GAP/2 of empty space around the
# centered icon and adjacent cells abut, so two adjacent thumbs are
# THUMB_GAP apart visually. The overlay is painted at the icon's rect, so
# the gap between overlays is the same as the gap between thumbnails.
THUMB_GAP = 8

CELL_WIDTH = THUMB_WIDTH + THUMB_GAP
CELL_HEIGHT = THUMB_HEIGHT

# Margin around the whole strip (top/bottom/left/right, painted in the
# FilmstripWidget's distinct background color).
STRIP_MARGIN = 8

# Strip background is set via QSS against the `#filmstrip` object
# name — see the host app stylesheet (`papyri/ui/app.qss`).

# Note: total strip height is computed at runtime in FilmstripWidget.__init__
# because it needs the horizontal scrollbar's pixel extent, which only the
# active QStyle knows (and which varies by platform).


# ---- caption overlay (drawn by the default delegate) -------------------

_CAPTION_HEIGHT = 22
_CAPTION_GRADIENT_TOP = QColor(0, 0, 0, 0)         # transparent at top
_CAPTION_GRADIENT_BOTTOM = QColor(0, 0, 0, 150)    # ~60% black at bottom


def stem_of(file_name: str) -> str:
    """Filename stem (extension stripped). Safe for names with embedded dots."""
    return os.path.splitext(file_name)[0]


class CaptionDelegate(QStyledItemDelegate):
    """Default thumbnail delegate: draws a two-line caption (filename
    stem + EXIF line) as a gradient overlay along the bottom of each
    thumb. The gradient fades from transparent at the top to ~60% black
    at the bottom so the text reads over any thumbnail, light or dark.

    Captions are read from item.text() — FilmstripWidget sets it as
    "filename\\nf/X | 1/Y" when adding. Empty second line is fine (the
    EXIF row just renders blank).

    Subclasses extend `paint` to add additional decorations (e.g.
    CaptureFilmstrip's ★ on the chosen take). The helper `_thumb_rect`
    exposes where the icon is actually painted so overlays land on the
    thumb regardless of cell-vs-icon size differences."""

    def displayText(self, value, locale) -> str:
        # Suppress the standard text-below-thumb caption — the gradient
        # caption overlay in paint() replaces it.
        return ""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        super().paint(painter, option, index)
        item = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(item, ImageFileListItem):
            return
        if item.is_placeholder:
            self._paint_placeholder_spinner(painter, self._thumb_rect(option))
        else:
            self._paint_caption(painter, self._thumb_rect(option), item)

    @staticmethod
    def _paint_placeholder_spinner(painter: QPainter, thumb_rect: QRect) -> None:
        """Macos-style rotating spoke spinner painted directly into the
        cell. Angle derives from wall clock so all visible placeholders
        rotate in sync. Animation is driven by FilmstripWidget's
        repaint timer; this method just paints the current frame."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(thumb_rect.x() + thumb_rect.width() / 2,
                          thumb_rect.y() + thumb_rect.height() / 2)
        n = 10
        phase = int((time.time() * n) % n)
        for i in range(n):
            painter.save()
            painter.rotate(i * (360.0 / n))
            alpha = int(40 + 215 * ((i - phase) % n) / (n - 1))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(40, 40, 40, alpha))
            painter.drawRoundedRect(QRectF(3, -1.6, 8, 3.2), 1.5, 1.5)
            painter.restore()
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        """Size each item to the FULL grid cell. Without this, Qt sizes the
        item to ~iconSize and hugs it top-left, so all the gridSize>iconSize
        slack lands on the right/bottom — a thumb visibly left-shifted in the
        vertical rail. Filling the cell lets IconMode center the icon
        (horizontally) within the cell. No-op for the horizontal strip, whose
        cell height equals the icon height."""
        view = self.parent()
        lst = getattr(view, "image_file_list", None)
        grid = lst.gridSize() if lst is not None else QSize()
        return grid if grid.isValid() else super().sizeHint(option, index)

    @staticmethod
    def _thumb_rect(option: QStyleOptionViewItem) -> QRect:
        """Where the icon actually lands in the cell, so the caption/overlays
        computed off this rect sit on the thumb. This mirrors how IconMode
        paints the icon: horizontally centered, top-anchored.

        VORLÄUFIG: in the vertical rail the taller cell (gridSize.h > iconSize.h)
        leaves vertical slack that IconMode drops below the thumb, so the thumb
        is horizontally centered but NOT vertically centered. sizeHint() (fill
        the cell) fixes the horizontal centering; vertical centering is still
        open — an in-app attempt to hand-paint the thumb centered showed no
        visible change and was removed. Likely next tweak: paint the icon
        ourselves at a both-axes-centered rect (needs HasDecoration cleared in
        initStyleOption) and set y back to the centered value below."""
        cell = option.rect
        icon = option.decorationSize
        x = cell.x() + (cell.width() - icon.width()) // 2
        y = cell.y()  # top-anchored, matching IconMode (see VORLÄUFIG above)
        return QRect(x, y, icon.width(), icon.height())

    @staticmethod
    def _paint_caption(
        painter: QPainter, thumb_rect: QRect, item: ImageFileListItem
    ) -> None:
        """Single-line caption: the capture's trailing index (e.g. "017").
        Full filename + EXIF live in the item's tooltip — set in
        FilmstripWidget.__add_image_item — so the thumb stays
        readable at strip scale."""
        strip = QRect(
            thumb_rect.x(),
            thumb_rect.bottom() - _CAPTION_HEIGHT + 1,
            thumb_rect.width(),
            _CAPTION_HEIGHT,
        )

        painter.save()
        gradient = QLinearGradient(
            float(strip.x()), float(strip.y()),
            float(strip.x()), float(strip.bottom()),
        )
        gradient.setColorAt(0.0, _CAPTION_GRADIENT_TOP)
        gradient.setColorAt(1.0, _CAPTION_GRADIENT_BOTTOM)
        painter.fillRect(strip, gradient)

        painter.setPen(QColor("white"))
        font = painter.font()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        # Left-elide so the distinguishing tail of a long filename stays
        # visible (e.g. "…12345_vis_001.JPG"). No-op for short captions
        # like the trailing index.
        text = painter.fontMetrics().elidedText(
            item.text(), Qt.TextElideMode.ElideLeft, strip.width() - 8,
        )
        painter.drawText(strip, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


# ---- model items -------------------------------------------------------

def get_file_index(file_path: str) -> Optional[int]:
    """Extract the trailing integer in a filename's stem (e.g. `..._001`
    → 1). Returns None if no digits found — those files are skipped."""
    import re
    basename = os.path.splitext(file_path)[0]
    numbers_in_basename = re.findall(r'\d+', basename)
    return int(numbers_in_basename[-1]) if numbers_in_basename else None


class ImageFileListItem(QListWidgetItem):
    """One thumbnail in the strip. Holds the absolute path and the parsed
    numeric index for sorting. The thumbnail itself is stored as the
    item's QIcon (via setIcon) — not as a separate attribute, because
    QIcon respects iconSize/decorationSize whereas a raw QPixmap/QImage
    returned from data(DecorationRole) paints at the cell's full rect,
    which would defeat the gridSize > iconSize inter-thumb gap. Custom
    delegates can read this back via `index.data(Qt.ItemDataRole.UserRole)`."""

    def __init__(self, path: str, thumbnail: QPixmap | QImage | None = None,
                 *, is_placeholder: bool = False):
        super().__init__()
        self.path: str = path
        self.file_name = Path(path).name
        self.index = get_file_index(self.file_name)
        self.is_placeholder: bool = is_placeholder
        if thumbnail is not None:
            pixmap = (QPixmap.fromImage(thumbnail)
                      if isinstance(thumbnail, QImage) else thumbnail)
            self.setIcon(QIcon(pixmap))

    def __lt__(self, other):
        return self.index < other.index

    def data(self, role: Qt.ItemDataRole):
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

    # Emitted right before a cache-miss full-decode worker is queued.
    # Lets the viewer show a busy spinner during the wait — paired with
    # image_decoded which arrives when the decode completes (and which
    # auto-hides the spinner via show_image).
    image_decode_started = pyqtSignal(str)      # path

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

        # Distinct background so STRIP_MARGIN is visible against the parent
        # window. The actual color is set via the host app stylesheet
        # against the `#filmstrip` object name — palette-driven
        # `setPalette` was tried but doesn't re-flip on dark/light
        # toggle (palette is sampled once at __init__). QSS re-applies
        # automatically via `install_app_stylesheet`'s colorSchemeChanged
        # hook. The QListWidget below is transparent so this bg shows
        # through the margin and the inter-thumb gap.
        self.setObjectName("filmstrip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        self._strip_layout = layout
        # contentsMargins are orientation-dependent (the vertical rail uses a
        # larger margin) — set in _configure_orientation().

        self.image_file_list = QListWidget(self)
        # IconMode + gridSize: cells are exactly gridSize, items flow
        # left-to-right in one row. Without IconMode (default ListMode),
        # gridSize isn't honored the same way and cells get stretched,
        # which breaks the delegate's overlay math.
        self.image_file_list.setViewMode(QListView.ViewMode.IconMode)
        self.image_file_list.setUniformItemSizes(True)
        self.image_file_list.setMovement(QListView.Movement.Static)
        self.image_file_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.image_file_list.setLayoutMode(QListView.LayoutMode.Batched)
        # AlignHCenter only — including AlignJustify makes Qt distribute
        # items edge-to-edge across the viewport, silently zeroing the
        # inter-thumb gap from gridSize.
        self.image_file_list.setItemAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.image_file_list.setWrapping(False)
        # Flow, scroll axis and scrollbar policies depend on the orientation
        # and are applied in _configure_orientation() (below).
        # Inter-thumb gap is manufactured via gridSize > iconSize: each
        # cell has THUMB_GAP/2 of empty space around the centered icon,
        # and adjacent cells abut, so two adjacent thumbs are THUMB_GAP
        # apart visually. setSpacing is NOT used here because in this
        # Flow=LeftToRight + Wrapping=False configuration Qt only honors
        # it vertically — and it does so by shrinking option.rect.height
        # to viewport.height - 2*spacing, which clips the icon.
        self.image_file_list.setIconSize(QSize(THUMB_WIDTH, THUMB_HEIGHT))
        # gridSize (the inter-thumb gap) is orientation-dependent — set in
        # _configure_orientation().
        self.image_file_list.setSpacing(0)
        self.image_file_list.setFrameShape(QFrame.Shape.NoFrame)
        # Transparent so STRIP_BG_COLOR shows through the contentsMargins
        # (the visible strip margin) and through the cell-vs-icon padding
        # (the visible inter-thumb gap). Rule lives in papyri/ui/app.qss
        # against the #filmstripList object name.
        self.image_file_list.setObjectName("filmstripList")
        layout.addWidget(self.image_file_list)

        # The horizontal scrollbar shows up inside the QListWidget viewport
        # only when content overflows. To keep the visible bottom margin at
        # exactly STRIP_MARGIN in BOTH states (no-scrollbar and scrollbar
        # visible), the FilmstripWidget grows by the scrollbar's pixel
        # extent whenever scrolling is needed, and shrinks back when it
        # isn't. Pixel extent is platform-dependent (query from the active
        # style). The horizontal scrollbar's `rangeChanged` signal fires
        # whenever the scrollable range changes — range == (0, 0) means
        # everything fits and the scrollbar is hidden; otherwise it's
        # shown. This is more reliable than Show/Hide event filtering,
        # which misses the initial render and fires on parent visibility
        # changes.
        self._scrollbar_extent = self.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent
        )
        # Orientation is configurable (default horizontal = a bottom strip,
        # unchanged for existing callers incl. papyri's CaptureFilmstrip). The
        # RTI app switches to vertical for a side rail via set_orientation().
        self._orientation = Qt.Orientation.Horizontal
        self._configure_orientation()

        # Directory binding state
        self.__currentPath: str | None = None
        self.__currentFileSet: set[str] = set()

        # Initial-load state. While `_initial_load_done` is False, the
        # dispatcher uses ImageMode.THUMB for most files (silent, fast
        # cache-aware load) and ImageMode.FULL for ONE file — the
        # preferred-stem match (caller-supplied via `open_directory`)
        # or, as a fallback, the last file in sort order. That one
        # file's full-image result is what lands in the viewer at
        # end of load. After `directory_loaded` fires, the flag flips
        # and subsequent file arrivals (new captures from the camera
        # worker) use ImageMode.FULL so they show in the viewer too.
        self._initial_load_done: bool = False
        self._preferred_stem: str | None = None
        # On-thumb caption text source. "index" (default) shows the
        # trailing capture number (e.g. "017") — right for fixed-sequence
        # workflows like RTI. "name" shows the full filename, left-elided
        # so the distinguishing tail stays visible. Opt-in via
        # set_caption_mode (papyri uses "name").
        self._caption_mode: str = "index"

        # Async thumbnail loading — shared global QThreadPool so filmstrip
        # workers and the bucket-selector's chosen-thumb workers compete
        # over one budget (capped at idealThreadCount() by Qt). Bounded
        # peak memory: at most one FULL-mode worker is in flight per
        # initial directory load (the preferred-or-last file); the rest
        # are THUMB workers (~30 MB peak each).
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

        # Default delegate: caption overlay (filename + EXIF) painted as
        # a gradient strip along the bottom of each thumb. Subclasses can
        # override via set_item_delegate() — CaptureFilmstrip does so to
        # add the chosen-take ★ on top of the caption.
        self.image_file_list.setItemDelegate(CaptionDelegate(self))

        # Subclass extension points
        self.__ctx_menu_provider: Optional[Callable[[ImageFileListItem], Optional[QMenu]]] = None

        # Drives the delegate-painted placeholder spinner. Runs only
        # while at least one placeholder exists; viewport.update()
        # each tick lets the delegate paint a fresh frame.
        self._placeholder_anim_timer = QTimer(self)
        self._placeholder_anim_timer.setInterval(80)
        self._placeholder_anim_timer.timeout.connect(
            self.image_file_list.viewport().update
        )

        # Flat-grey pixmap reused by every placeholder. Built once
        # here (QPixmap needs QGuiApplication so it can't live at
        # class scope); implicit-sharing keeps the memory cost flat
        # regardless of placeholder count.
        self._placeholder_pixmap = QPixmap(THUMB_WIDTH, THUMB_HEIGHT)
        self._placeholder_pixmap.fill(QColor(220, 220, 220))

        self.image_file_list.currentItemChanged.connect(self.__on_select_image_file)

    # ---- public API ----------------------------------------------------

    def set_caption_mode(self, mode: str) -> None:
        """Set the on-thumb caption source: "index" (trailing number) or
        "name" (full filename, left-elided). Affects items added after
        this call — set it once before open_directory."""
        self._caption_mode = mode

    def open_directory(self, dir_path: str, *,
                       preferred_stem: str | None = None) -> None:
        """Open a new directory: close any currently-open one, start
        watching, kick off the initial async thumb load.

        `preferred_stem` (optional): the basename-without-extension of
        the file that should land in the viewer at end of load. The
        dispatcher uses ImageMode.FULL for the matching file and
        ImageMode.THUMB for everything else. If None (or the stem
        doesn't match any file in this directory), the dispatcher
        falls back to the last file in sort order. Used by the
        papyri filmstrip to surface the bucket's chosen-take when
        the object opens."""
        if self.__currentPath:
            self.close_directory()
        self._initial_load_done = False
        self._preferred_stem = preferred_stem
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
        # Don't try to clear queued workers — the pool is shared
        # (QThreadPool.globalInstance()), so clear() would drop other
        # widgets' queued workers too (e.g. bucket-selector chosen-thumb
        # loads). Running workers will finish on their own; results
        # whose gen doesn't match the bumped generation are silently
        # dropped in __on_image_loaded. close returns promptly without
        # waitForDone() so the UI stays responsive on rapid switches.
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

    def num_files(self) -> int:
        """Number of items in the strip (files, plus any transient
        drop-import placeholders)."""
        return self.image_file_list.count()

    def files(self) -> list[str]:
        """Paths of all items in the strip, in display order."""
        return [self.image_file_list.item(row).path
                for row in range(self.image_file_list.count())]

    def last_index(self) -> int:
        """Capture index (trailing number) of the last item, or 0 if the
        strip is empty. The RTI app uses this to persist the preview count."""
        count = self.image_file_list.count()
        if count > 0:
            return self.image_file_list.item(count - 1).index
        return 0

    def show_current(self) -> Optional[str]:
        """Re-display the currently-selected thumb in the viewer.
        Emits image_decoded (cache hit) or image_decode_started +
        queues a fresh decode (cache miss). Does NOT emit
        image_selected — this is a caller-initiated refresh, not a
        user click; the caller updates any derived UI state itself.
        Returns the current item's basename, or None if nothing is
        selected. Used to "refresh" the viewer when something other
        than a thumb click should re-assert the selection — e.g.
        pausing live view, so the viewer stops showing the stale
        last live frame and shows the selected take instead."""
        current = self.image_file_list.currentItem()
        if not isinstance(current, ImageFileListItem):
            return None
        cached_image = QPixmapCache.find(current.path)
        if cached_image:
            self.image_decoded.emit(current.path, cached_image)
        else:
            self.image_decode_started.emit(current.path)
            self.__load_image(current.path, self.__show_and_cache)
        return current.file_name

    def reload_current(self) -> None:
        """Re-decode the current item from disk and refresh BOTH its
        thumbnail and the viewer. Call after the file's bytes changed on
        disk (e.g. its EXIF Orientation was rewritten). The disk thumb cache
        self-invalidates (it keys on mtime), but the in-memory full-image
        cache keys on path only — so drop it explicitly first."""
        current = self.image_file_list.currentItem()
        if not isinstance(current, ImageFileListItem):
            return
        QPixmapCache.remove(current.path)
        self.image_decode_started.emit(current.path)
        self.__load_image(current.path, self.__apply_reload, ImageMode.FULL)

    def __apply_reload(self, result: LoadImageWorkerResult) -> None:
        """Reload result: refresh the current item's thumbnail icon and the
        viewer (FULL mode yields both). Guards against the selection having
        moved while the decode ran."""
        current = self.image_file_list.currentItem()
        if not (isinstance(current, ImageFileListItem)
                and current.path == result.path):
            return
        if result.thumbnail is not None:
            thumb = (QPixmap.fromImage(result.thumbnail)
                     if isinstance(result.thumbnail, QImage)
                     else result.thumbnail)
            current.setIcon(QIcon(thumb))
        if result.image is not None:
            pixmap = QPixmap.fromImage(result.image)
            QPixmapCache.insert(result.path, pixmap)
            self.image_decoded.emit(result.path, pixmap)

    def scroll_to_end(self) -> None:
        """Scroll horizontally so the last item is visible. Used after
        drop-import so the user sees the spinner placeholders that
        were just inserted.

        Deferred one tick because the list lays out newly-added items
        asynchronously — `horizontalScrollBar().maximum()` doesn't
        reflect the new content until layout settles. Calling
        `scrollToBottom` on a horizontal-flow list isn't reliable
        across Qt versions, so we drive the scrollbar directly."""
        def _scroll():
            bar = (self.image_file_list.verticalScrollBar()
                   if self._orientation == Qt.Orientation.Vertical
                   else self.image_file_list.horizontalScrollBar())
            bar.setValue(bar.maximum())
        QTimer.singleShot(0, _scroll)

    def __find_placeholder(self, path: str) -> Optional["ImageFileListItem"]:
        for row in range(self.image_file_list.count()):
            item = self.image_file_list.item(row)
            if (isinstance(item, ImageFileListItem)
                    and item.is_placeholder and item.path == path):
                return item
        return None

    def _start_placeholder_anim(self) -> None:
        if not self._placeholder_anim_timer.isActive():
            self._placeholder_anim_timer.start()

    def _stop_placeholder_anim_if_done(self) -> None:
        for row in range(self.image_file_list.count()):
            item = self.image_file_list.item(row)
            if (isinstance(item, ImageFileListItem) and item.is_placeholder):
                return
        self._placeholder_anim_timer.stop()

    def add_placeholder(self, path: str) -> None:
        """Insert a placeholder for an incoming file. When the real
        thumb later arrives via the FS watcher, `__add_image_item`
        removes this placeholder and inserts a fresh item at the
        sorted position. Used by the papyri drop-import flow to give
        the user instant visual feedback before the file copy + decode
        complete.

        The placeholder icon is a flat-grey pixmap shared across all
        placeholder instances (see `_placeholder_pixmap` init) — it
        matches the uniform cell size, so QListWidget's
        `setUniformItemSizes(True)` cache stays correct. The animated
        spoke-spinner overlay is painted by the delegate; a QTimer
        drives viewport repaints while placeholders exist."""
        item = ImageFileListItem(path, self._placeholder_pixmap,
                                 is_placeholder=True)
        with QMutexLocker(self.__mutex):
            if not self.__currentPath:
                return
            self.image_file_list.addItem(item)
            self.image_file_list.sortItems()
        self._start_placeholder_anim()
        # Scroll here, at insert time, rather than later when the
        # decoder swaps in the real thumb — the placeholder is the
        # user-visible signal that "your file is incoming," so it
        # should be onscreen the moment it appears.
        self.scroll_to_end()

    def remove_placeholder(self, path: str) -> None:
        """Drop the placeholder for `path` if one exists. No-op
        otherwise. Used when an incoming copy fails — the placeholder
        was seeded synchronously but no real thumb will ever arrive."""
        with QMutexLocker(self.__mutex):
            placeholder = self.__find_placeholder(path)
            if placeholder is None:
                return
            self.image_file_list.takeItem(
                self.image_file_list.row(placeholder)
            )
        self._stop_placeholder_anim_if_done()

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

    # ---- orientation + scrollbar-aware cross-axis size ----------------

    def set_orientation(self, orientation: Qt.Orientation) -> None:
        """Lay the strip out horizontally (default — a bottom strip) or
        vertically (a side rail). Lives in the base so either app can pick
        either; the default is horizontal so existing callers (incl. papyri's
        CaptureFilmstrip) are unaffected."""
        if orientation == self._orientation:
            return
        self._orientation = orientation
        self._configure_orientation()

    def _configure_orientation(self) -> None:
        """Apply the flow, scroll axis and cross-axis size that depend on the
        orientation. Called from __init__ and set_orientation."""
        lst = self.image_file_list
        vertical = self._orientation == Qt.Orientation.Vertical
        # Vertical rail: NO outer margin so the strip sits flush with the viewer
        # (top/bottom/left); horizontal strip keeps STRIP_MARGIN (unchanged for
        # papyri).
        self._strip_margin = 0 if vertical else STRIP_MARGIN
        self._strip_layout.setContentsMargins(
            self._strip_margin, self._strip_margin,
            self._strip_margin, self._strip_margin)
        # Gap around each thumbnail (cell padding beyond the icon): doubled for
        # the vertical rail. The gap is the stacking-axis component of
        # gridSize − iconSize, so a taller cell = more space between stacked
        # thumbs; a wider cell = more side padding.
        gap = 2 * THUMB_GAP if vertical else THUMB_GAP
        self._cell_w = THUMB_WIDTH + gap
        self._cell_h = THUMB_HEIGHT + (gap if vertical else 0)
        lst.setGridSize(QSize(self._cell_w, self._cell_h))
        lst.setFlow(QListView.Flow.TopToBottom if vertical
                    else QListView.Flow.LeftToRight)
        if vertical:
            lst.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
            lst.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            lst.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
            lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            lst.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # (Re)wire the scroll-axis rangeChanged → cross-axis resize; drop any
        # previous connection first so a re-orient can't double-fire.
        for bar in (lst.horizontalScrollBar(), lst.verticalScrollBar()):
            try:
                bar.rangeChanged.disconnect(self._on_scroll_range_changed)
            except TypeError:
                pass
        active_bar = lst.verticalScrollBar() if vertical else lst.horizontalScrollBar()
        active_bar.rangeChanged.connect(self._on_scroll_range_changed)
        self._apply_strip_extent(scrollbar_visible=False)

    def _apply_strip_extent(self, *, scrollbar_visible: bool) -> None:
        """Pin the strip's CROSS axis to one thumbnail cell + margins, growing
        by the scrollbar's extent when it shows so the visible margin stays at
        STRIP_MARGIN. Horizontal → fixed height (bottom strip); vertical →
        fixed width (side rail; height is free so it fills its column)."""
        extra = self._scrollbar_extent if scrollbar_visible else 0
        m = self._strip_margin
        if self._orientation == Qt.Orientation.Vertical:
            self.setFixedWidth(m + self._cell_w + extra + m)
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX — let the rail fill its column
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        else:
            self.setFixedHeight(m + self._cell_h + extra + m)

    def _on_scroll_range_changed(self, minimum: int, maximum: int) -> None:
        """Range == (0, 0) ⇔ no scrolling needed ⇔ scrollbar hidden under the
        AsNeeded policy. Anything else ⇒ scrollbar visible ⇒ grow the strip."""
        self._apply_strip_extent(scrollbar_visible=(maximum > minimum))

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
        """Diff disk against the last-known fileset; seed placeholders +
        queue decoders for added files; remove items for vanished files.
        Empty-diff emits `directory_loaded` immediately; otherwise the
        emit fires from `__on_image_loaded` when the last worker
        finishes."""
        added, removed = self.__diff_disk()
        if not added and not removed:
            self.__emit_directory_loaded()
            return
        self.__seed_placeholders(added)
        self.__queue_decoders(added)
        self.__remove_items(removed)

    def __diff_disk(self) -> tuple[set[str], set[str]]:
        """Re-read the watched directory; return `(added, removed)`
        filenames against the last-known fileset; update the fileset
        in place. A missing path is treated as empty (so stale items
        get cleared if the directory disappears)."""
        if os.path.isdir(self.__currentPath):
            new_files = [
                f for f in listdir(self.__currentPath)
                # Skip hidden / macOS AppleDouble sidecars (`._foo.ARW`):
                # they share the real file's extension and index, so they'd
                # otherwise show up as a duplicate thumbnail and fail decode.
                if not f.startswith(".")
                and Path(f).suffix.lower() in SUPPORTED_EXTENSIONS
                and get_file_index(f) is not None
            ]
        else:
            new_files = []
        new_fileset = set(new_files)
        added = new_fileset - self.__currentFileSet
        removed = self.__currentFileSet - new_fileset
        self.__currentFileSet = new_fileset
        return added, removed

    def __seed_placeholders(self, added: set[str]) -> None:
        """Seed a spinner placeholder per incoming file. Runs for
        initial load AND post-load arrivals — during initial load the
        placeholders establish the final sort order up front so thumbs
        fill in place rather than shuffling on each async insert.
        Idempotent against drop-import's synchronous pre-seed via
        `__find_placeholder`."""
        for f in sorted(added):
            full = os.path.join(self.__currentPath, f)
            if not self.__find_placeholder(full):
                self.add_placeholder(full)

    def __queue_decoders(self, added: set[str]) -> None:
        """Decide per file: THUMB (silent, fast) vs FULL (also pushes
        full image to viewer). After initial load every new arrival
        gets FULL — each is a fresh capture and should be auto-shown.
        During initial load exactly ONE file gets FULL (preferred-stem
        match or highest-index fallback) so the viewer settles on a
        single image instead of flashing through them."""
        if not added:
            return
        both_targets = self.__pick_both_targets(added)
        for f in sorted(added, key=lambda x: (get_file_index(x) or 0, x)):
            mode = ImageMode.FULL if f in both_targets else ImageMode.THUMB
            self.__load_image(f, self.__add_image_item, mode=mode)

    def __remove_items(self, removed: set[str]) -> None:
        """Take out list items whose files vanished from disk."""
        for f in removed:
            for i in range(self.image_file_list.count()):
                item = self.image_file_list.item(i)
                if isinstance(item, ImageFileListItem) and item.file_name == f:
                    self.image_file_list.takeItem(i)
                    del item
                    break

    def __emit_directory_loaded(self) -> None:
        """Mark initial load complete and emit `directory_loaded`
        deferred via QTimer — synchronous emit would land on slots
        whose connections are still being wired up by later receivers
        in the same chain."""
        path = self.__currentPath
        self._initial_load_done = True
        QTimer.singleShot(0, lambda: self.directory_loaded.emit(path))

    def __pick_both_targets(self, added_files: set[str]) -> set[str]:
        """Decide which files in this batch should get BOTH mode (full
        decode → viewer). After initial load: every new arrival (each
        is a fresh capture). During initial load: at most one file —
        the preferred-stem match, else the highest-index file.

        For a stem match with both JPG + RAW siblings, prefer the JPG
        (faster decode, same display quality for the viewer)."""
        if self._initial_load_done:
            return set(added_files)
        if not added_files:
            return set()
        # Initial load: pick exactly one.
        if self._preferred_stem is not None:
            matches = [f for f in added_files
                       if Path(f).stem == self._preferred_stem]
            jpegs = [f for f in matches
                     if Path(f).suffix.lower() in JPEG_EXTENSIONS]
            chosen = jpegs[0] if jpegs else (matches[0] if matches else None)
            if chosen is not None:
                return {chosen}
        # Fallback: the file with the highest index (= newest capture).
        ordered = sorted(added_files, key=lambda f: (get_file_index(f) or 0, f))
        return {ordered[-1]}

    def __load_image(
        self,
        file_name: str,
        on_finished_callback: Callable,
        mode: ImageMode = ImageMode.FULL,
    ) -> None:
        """Queue an async decode worker. Captures the current generation
        token so __on_image_loaded can drop stale results."""
        self.__num_images_to_load += 1

        worker = LoadImageWorker(
            os.path.join(self.__currentPath, file_name),
            mode=mode,
            thumb_max_size=200,
        )
        # Each call's `gen` is its own local — Python closures capture by
        # reference but the variable is re-bound per call so each lambda
        # closes over a distinct value.
        gen = self.__generation
        worker.signals.finished.connect(
            lambda result: self.__on_image_loaded(result, on_finished_callback, gen)
        )
        QThreadPool.globalInstance().start(worker)

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
            self.__emit_directory_loaded()
            # Re-scan in case files arrived during loading.
            self.__load_directory()

    def __add_image_item(self, result: LoadImageWorkerResult) -> None:
        """Create a list item for the file and add it to the strip.

        Result shape dictates behavior:
          - `result.thumbnail` is always set → drives the strip card.
          - `result.image` is set only for BOTH-mode arrivals
            (one preferred file during initial load, OR any newly
            arrived capture post-load). When set, this is the file
            we want the viewer to display — auto-select + cache +
            emit image_decoded. THUMB-only arrivals (the silent
            majority of initial loads) just land in the strip.

        If a placeholder item exists for this path (drop-import
        flow), it is removed before the fresh item is inserted;
        sortItems re-positions the new item. Mutating the placeholder
        in place was tried first but Qt's IconMode +
        setUniformItemSizes view-state cache mis-rendered re-used
        items.

        The currentItemChanged disconnect/reconnect around
        setCurrentItem keeps the user-click slot quiet during
        programmatic selection."""
        pixmap = (QPixmap.fromImage(result.thumbnail)
                  if isinstance(result.thumbnail, QImage)
                  else result.thumbnail)

        exposure_time = result.exif.get("ExposureTime")
        f_number = result.exif.get("FNumber")
        # On-thumb caption: either the trailing capture index ("017",
        # short, for fixed-sequence workflows) or the full filename
        # (left-elided by the delegate so the tail stays readable). Full
        # filename + EXIF always live in the tooltip below.
        idx = get_file_index(result.path)
        if self._caption_mode == "name":
            # Stem only — no extension (the .jpg/.raw suffix is noise on
            # the thumb; the full name incl. extension stays in the tooltip).
            caption = Path(result.path).stem
        else:
            caption = f"{idx:03d}" if idx is not None else Path(result.path).stem
        tooltip = Path(result.path).name
        if exposure_time is not None and f_number is not None:
            tooltip += f"\nf/{f_number} | {getattr(exposure_time, 'real', exposure_time)}"

        # Add the item only if a directory is still open (this callback
        # fires from a worker thread completion; the directory may have
        # been closed in the meantime — extra safety on top of the
        # generation-token gate in __on_image_loaded).
        with QMutexLocker(self.__mutex):
            if not self.__currentPath:
                return
            # Drop any existing placeholder for this path — fresh
            # ImageFileListItem rebuild is more reliable than mutating
            # an existing item in place (Qt's view-state caching in
            # IconMode + setUniformItemSizes can leave invalid sizing
            # for re-used items).
            placeholder = self.__find_placeholder(result.path)
            if placeholder is not None:
                self.image_file_list.takeItem(
                    self.image_file_list.row(placeholder)
                )
            list_item = ImageFileListItem(result.path, pixmap)
            list_item.setText(caption)
            list_item.setToolTip(tooltip)
            self.image_file_list.addItem(list_item)
            self.image_file_list.sortItems()
            self._stop_placeholder_anim_if_done()
            if result.image is None:
                # THUMB-only: silent fill, no viewer update.
                return
            # BOTH-mode arrival: auto-select + push to viewer.
            # Disconnect the specific slot (not all slots — a subclass
            # may have its own listener) so the programmatic select
            # doesn't fire image_selected.
            self.image_file_list.currentItemChanged.disconnect(
                self.__on_select_image_file
            )
            try:
                self.image_file_list.setCurrentItem(list_item)
            finally:
                self.image_file_list.currentItemChanged.connect(
                    self.__on_select_image_file
                )
            # Scroll only when no placeholder existed — placeholders
            # already scrolled at seed time. Initial-load preferred
            # file is the typical case here (no placeholder seeded
            # during initial load).
            if placeholder is None:
                self.scroll_to_end()

        pixmap = QPixmap.fromImage(result.image)
        # Cache so a subsequent click on this thumb skips the full
        # decode and shows instantly.
        QPixmapCache.insert(result.path, pixmap)
        self.image_decoded.emit(result.path, pixmap)

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
            self.image_decode_started.emit(file_path)
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
