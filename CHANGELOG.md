# Changelog

All versions of this fork were developed by Claude (AI model by Anthropic) based on the requirements and testing of simonsbg-droid.

## 2.4.0 – 2026-10-31

### New
- Sorting by name, modified date, size (folders: number of items) or type; ascending/descending; folders first on/off
- Sort button in the toolbar, "Sort by" in the right-click menu of an empty area, Ctrl+Alt+1–4 (press again to reverse)
- One default sorting for all folders (also in the settings), individual folders can keep their own sorting (saved in `~/.config/nemo-miller-columns/sortierung.json`)
- Natural sort order independent of the system language: numbers by value, accented letters with their base letter, case-insensitive
- File preview creates missing thumbnails for PDFs and videos too

## 2.3.0 – 2026-10-31

### New
- Gallery view (Ctrl+G, grid button in the toolbar): thumbnails of images, videos and PDFs in a grid, keyboard navigation incl. Shift+arrow selection, info bar, context menu, drag & drop and file operations as in the column view
- Picture folders (mostly images/videos) open as a gallery automatically; can be turned off; thumbnail size adjustable in the settings
- Thumbnails use the shared freedesktop cache (`~/.cache/thumbnails`); missing ones are created in the background, images directly and other types with the installed system thumbnailers
- Preview moved into the column view: the slot right of the working column shows folder contents with a footer (items, folder size, last change) or a file preview with more details (created/modified/opened, dimensions, size in bytes)
- Folder sizes are calculated in the background, with live progress and caching
- Column widths by need: columns get the width their longest name needs; spare space goes to columns that would otherwise shorten names
- Path bar always shows the current folder at its end, the home folder as a house icon, very long folder names shortened in the middle

### Fixed
- Backspace in an empty folder went up two levels

## 2.2.0 – 2026-10-31

### New
- Settings window (menu button or Ctrl+Comma): font size, row spacing, hidden files, start folder, column width slider; changes apply instantly to all windows and are saved in `~/.config/nemo-miller-columns/einstellungen.json`
- Weighted column widths: the working column gets wider, columns further left narrower (adjustable from "equal" to "maximum emphasis")
- Names are shortened in the middle so file extensions stay visible; tooltip with the full name when shortened
- Double-click on a column divider fits the column to its longest name
- Ctrl+Plus / Ctrl+Minus / Ctrl+0 change the font size; icon size follows the font
- Keyboard shortcut overview in the menu
- Icon redesigned in the style of the Mint-Y folder (same shape, so it lines up with other panel icons)

### Changed
- Row spacing is now a slider from "very tight" (no padding, icons shrink to text height) to "very loose"; the previous setting is migrated

### Fixed
- Column widths no longer jump or flicker while moving through a column with the arrow keys: widths are calculated before drawing, and a stable slot is reserved to the right of the working column

## 2.1.0 – 2026-10-30

### New
- Own application icon (panel, menu, window switcher)
- Window title shows the current folder ("Folder – Spaltenansicht")
- Extract archives from the right-click menu: "Extract here" and "Extract to folder"; ZIP and TAR built in (safe against path traversal, Windows file name encodings), other formats and password-protected ZIPs via file-roller; an archive with a single top-level folder is not nested twice
- "New folder" button in the toolbar
- Ctrl+L turns the path bar into an editable field with folder completion (also: click on the empty part of the path bar)

## 2.0.0 – 2026-10-01

First release of this fork, based on [davemin/linux-nemo-miller-columns](https://github.com/davemin/linux-nemo-miller-columns).

### New
- Keyboard navigation: arrow keys move through columns, Enter, Home/End, type-to-jump
- Shortcuts: Esc (start folder), Ctrl+W, Ctrl+N, Ctrl+L, F5, Ctrl+H, Alt+Home, Backspace/Alt+Up
- File operations: copy, cut, paste, rename (Ctrl+U or F2), new folder (Ctrl+Shift+N), trash (Delete), delete permanently (Shift+Delete)
- Conflict dialog: skip, keep both, replace (old version goes to the trash)
- Clipboard compatible with Nemo (`x-special/gnome-copied-files`)
- Drag & drop within the view and with other applications
- Multi-selection (Ctrl/Shift+click), right-click menu
- "Open with": submenu with recommended applications, "Other application …" dialog (Ctrl+O), option to set as default
- Folders update automatically when changed by other programs
- Configurable start folder (`~/.config/nemo-miller-columns/startordner`) shown as the leftmost column, with a "Start" button
- German user interface; Nemo menu entry in German or English depending on system language

### Fixed
- Columns were shown twice when opening a folder
- Window could not be snapped or maximized (minimum size grew with the number of columns and the preview)
- Many columns now scroll sideways instead of widening the window
- Separate panel icon with a fallback gear symbol; window now matches its launcher
- "New Window" from the panel and Ctrl+N
- A single click on a file opened it; now a double-click is needed
- Esc closed the window
