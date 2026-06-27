Set objShell = CreateObject("Shell.Application")
objShell.ShellExecute "cmd.exe", _
    "/c ""D:/ProjectRoot/PythonProject/screenshot-lens\screenshot-search.cmd""", _
    "D:/ProjectRoot/PythonProject/screenshot-lens", _
    "runas", 0


