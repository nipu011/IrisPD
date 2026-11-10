# IrisPD v14 - Standalone Windows executable

The standalone build is a **single `IrisPD.exe`** that runs on 64-bit Windows 10/11 without installing Python on the target computer.

## Option A - Build on your Windows computer

1. Extract all files from this v14 ZIP into a permanent folder.
2. Install **64-bit Python 3.11** on the **build** computer (not needed on the target computer). You must have internet access to install the pinned dependencies.
3. Double-click `Build_Windows_Standalone_EXE.bat`.
4. After it finishes, copy `dist\IrisPD.exe` to the computer where you want to use the app.
5. Double-click `IrisPD.exe`. It bundles the PDF help, MediaPipe assets and app icon. The app still needs webcam permissions.

Building may require several gigabytes of disk space temporarily. One-file startup can be slower than a folder distribution. Windows SmartScreen may warn about unsigned executables; only run binaries whose origin you trust. On some systems, installing the Microsoft Visual C++ runtime may be necessary.

## Option B - Build without Python installed locally, using GitHub Actions

1. Create a GitHub repository and upload **the contents of the IrisPD_Windows_v14 folder**, including the hidden `.github/workflows/windows-standalone.yml` file.
2. Open the repository's **Actions** tab. Select **Build standalone IrisPD for Windows**.
3. Click **Run workflow**. After it succeeds, download the `IrisPD-Windows-standalone` artifact and extract `IrisPD.exe`.
4. Test the executable and, before public distribution, consider code-signing it.

GitHub Actions is an external build service; execution requires your own GitHub account and may be subject to service usage limits. No GitHub login is needed to build via Option A.

## Existing folder-based executable

`Build_Windows_EXE.bat` still builds `dist\IrisPD\IrisPD.exe` (the entire containing folder must be copied). This starts faster than the single-file version in many setups.

## What is included in this download

The downloadable ZIP includes the updated application source, two Windows EXE builders, a GitHub Windows build workflow, tests, and a help PDF. **It does not include a precompiled Windows executable** because the build environment here cannot run Windows packaging tools. The scripts and workflow have not yet been tested against a physical Windows installation.
