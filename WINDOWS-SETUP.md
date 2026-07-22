# Running byzanz-capture from source on Windows

The RTI app can only run on Windows inside **MSYS2 / MINGW64** — this is not a
choice but a requirement: it drives the camera through **libgphoto2**, which has
no native Windows build and no Windows Python wheel. MSYS2 provides both
`libgphoto2` and a Python that can use it. (If you only want to *run* the app,
not edit it, skip all of this and use the frozen build — see the bottom.)

Everything below is scripted in `scripts/vm-win-setup.sh`, so it stays in
lockstep with the Windows CI. Run the phases in order.

## Prerequisites (install once)

1. **GitHub Desktop** — to clone/pull/push the repository (you have this).
2. **MSYS2** — <https://www.msys2.org>. Run the installer and let it finish its
   initial package update.

## Setup (in this order)

1. **Clone the repo** in GitHub Desktop (`cceh/byzanz-capture`). It lands under
   `Documents\GitHub\byzanz-capture` by default. In GitHub Desktop,
   *Repository → Show in Explorer* shows the exact folder.

2. **Open the `MSYS2 MINGW64` shell** — Start menu → **“MSYS2 MINGW64”**
   (the blue icon). ⚠️ *Not* the default “MSYS2 MSYS” shell — the wrong shell
   uses the wrong Python and nothing below will work.

3. **Go to the repo** (translate the Windows path to an MSYS path — drive `C:`
   becomes `/c`):
   ```bash
   cd /c/Users/<YOU>/Documents/GitHub/byzanz-capture
   ```

4. **Install the MINGW packages** (libgphoto2, PyQt6, numpy, opencv, …):
   ```bash
   ./scripts/vm-win-setup.sh deps
   ```
   > If `deps` complains that packages can't be found, the package DB is stale —
   > run `./scripts/vm-win-setup.sh sync` once (full update; reopen the shell if
   > it asks), then `deps` again.

5. **Create the Python environment** (`.venv` + build `python-gphoto2` from
   source against the MINGW libgphoto2):
   ```bash
   ./scripts/vm-win-setup.sh venv
   ```

6. **Run the app:**
   ```bash
   ./scripts/vm-win-setup.sh run
   ```

That's it. Steps 4–5 are one-time (repeat only after a dependency change);
day-to-day you just edit the code and re-run **step 6**.

## Camera control needs a one-time driver swap

The app will *detect* the camera immediately, but Windows binds its own MTP
driver to it, so libgphoto2 can't *control* it yet (error `-53 "Could not claim
the USB device"`). Fix it **once per camera model** by switching that device to
the **WinUSB** driver with **[Zadig](https://zadig.akeo.ie)** (needs admin).
Background and a planned in-app one-click flow:
`docs/windows-winusb-driver-autosetup-concept.md`.

## Where the app writes data (not in the repo folder)

Running from source does **not** write into the repo. Runtime data goes to the
user profile:

- **Settings** → Windows Registry `HKCU\Software\CCeH\Byzanz RTI`
- **Logs / crash.log** → `%LOCALAPPDATA%\ByzanzCapture\Logs`
- **Sessions / images / .lp** → the working directory you pick in Settings
  (default: your `Pictures` folder)

## Editing the code

Edit with whatever you like (an editor, or Claude Code in a second terminal),
then commit and **push via GitHub Desktop**. The two shells are independent:
edit/commit anywhere, but always **run** the app from the *MINGW64* shell.

## Alternative: just run it, no MSYS2

If you don't need to run from source, grab the built app instead: GitHub →
**Actions → “Build for Windows”** → a green run → download the
**`byzanz-capture-windows`** artifact, unzip it, and run
`byzanz-capture.exe`. No install, no MSYS2 — the camera driver step above still
applies.
