Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "d:\Gmail & Calendar Agent"
WScript.Quit shell.Run("python.exe bot.py", 0, True)
