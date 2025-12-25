-- Minimal wrapper to launch the Python bootstrap in the app bundle.
set appPath to POSIX path of (path to me)
set bootstrapPath to appPath & "Contents/Resources/launcher_bootstrap.py"
do shell script "/usr/bin/python3 " & quoted form of bootstrapPath
